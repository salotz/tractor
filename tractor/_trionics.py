"""
``trio`` inspired apis and helpers
"""
import multiprocessing as mp
import inspect

import trio
from async_generator import asynccontextmanager, aclosing

from ._state import current_actor
from .log import get_logger, get_loglevel
from ._actor import Actor, ActorFailure
from ._portal import Portal


ctx = mp.get_context("forkserver")
log = get_logger('tractor')


class ActorNursery:
    """Spawn scoped subprocess actors.
    """
    def __init__(self, actor, supervisor=None):
        self.supervisor = supervisor  # TODO
        self._actor = actor
        self._children = {}
        # portals spawned with ``run_in_actor()``
        self._cancel_after_result_on_exit = set()
        self.cancelled = False

    async def __aenter__(self):
        return self

    async def start_actor(
        self,
        name: str,
        bind_addr=('127.0.0.1', 0),
        statespace=None,
        rpc_module_paths=None,
        loglevel=None,  # set log level per subactor
    ):
        loglevel = loglevel or self._actor.loglevel or get_loglevel()
        actor = Actor(
            name,
            # modules allowed to invoked funcs from
            rpc_module_paths=rpc_module_paths or [],
            statespace=statespace,  # global proc state vars
            loglevel=loglevel,
            arbiter_addr=current_actor()._arb_addr,
        )
        parent_addr = self._actor.accept_addr
        assert parent_addr
        proc = ctx.Process(
            target=actor._fork_main,
            args=(bind_addr, parent_addr),
            # daemon=True,
            name=name,
        )
        # register the process before start in case we get a cancel
        # request before the actor has fully spawned - then we can wait
        # for it to fully come up before sending a cancel request
        self._children[actor.uid] = [actor, proc, None]

        proc.start()
        if not proc.is_alive():
            raise ActorFailure("Couldn't start sub-actor?")

        log.info(f"Started {proc}")
        # wait for actor to spawn and connect back to us
        # channel should have handshake completed by the
        # local actor by the time we get a ref to it
        event, chan = await self._actor.wait_for_peer(actor.uid)
        portal = Portal(chan)
        self._children[actor.uid][2] = portal
        return portal

    async def run_in_actor(
        self,
        name,
        fn,
        bind_addr=('127.0.0.1', 0),
        rpc_module_paths=None,
        statespace=None,
        loglevel=None,  # set log level per subactor
        **kwargs,  # explicit args to ``fn``
    ):
        """Spawn a new actor, run a lone task, then terminate the actor and
        return its result.

        Actors spawned using this method are kept alive at nursery teardown
        until the task spawned by executing ``fn`` completes at which point
        the actor is terminated.
        """
        mod_path = fn.__module__
        portal = await self.start_actor(
            name,
            rpc_module_paths=[mod_path],
            bind_addr=bind_addr,
            statespace=statespace,
        )
        await portal._submit_for_result(
            mod_path,
            fn.__name__,
            **kwargs
        )
        self._cancel_after_result_on_exit.add(portal)
        return portal

    async def wait(self):
        """Wait for all subactors to complete.
        """
        async def wait_for_proc(proc, actor, portal):
            # TODO: timeout block here?
            if proc.is_alive():
                await trio.hazmat.wait_readable(proc.sentinel)
            # please god don't hang
            proc.join()
            log.debug(f"Joined {proc}")
            self._children.pop(actor.uid)

        async def wait_for_result(portal, actor):
            # cancel the actor gracefully
            log.info(f"Cancelling {portal.channel.uid} gracefully")
            await portal.cancel_actor()

            log.debug(f"Waiting on final result from {subactor.uid}")
            res = await portal.result()
            # if it's an async-gen then we should alert the user
            # that we're cancelling it
            if inspect.isasyncgen(res):
                log.warn(
                    f"Blindly consuming asyncgen for {actor.uid}")
                with trio.fail_after(1):
                    async with aclosing(res) as agen:
                        async for item in agen:
                            log.debug(f"Consuming item {item}")

        # unblocks when all waiter tasks have completed
        children = self._children.copy()
        async with trio.open_nursery() as nursery:
            for subactor, proc, portal in children.values():
                nursery.start_soon(wait_for_proc, proc, subactor, portal)
                if proc.is_alive() and (
                    portal in self._cancel_after_result_on_exit
                ):
                    nursery.start_soon(wait_for_result, portal, subactor)

    async def cancel(self, hard_kill=False):
        """Cancel this nursery by instructing each subactor to cancel
        iteslf and wait for all subprocesses to terminate.

        If ``hard_killl`` is set to ``True`` then kill the processes
        directly without any far end graceful ``trio`` cancellation.
        """
        def do_hard_kill(proc):
            log.warn(f"Hard killing subactors {self._children}")
            proc.terminate()
            # XXX: below doesn't seem to work?
            # send KeyBoardInterrupt (trio abort signal) to sub-actors
            # os.kill(proc.pid, signal.SIGINT)

        log.debug(f"Cancelling nursery")
        with trio.fail_after(3):
            async with trio.open_nursery() as n:
                for subactor, proc, portal in self._children.values():
                    if hard_kill:
                        do_hard_kill(proc)
                    else:
                        if portal is None:  # actor hasn't fully spawned yet
                            event = self._actor._peer_connected[subactor.uid]
                            log.warn(
                                f"{subactor.uid} wasn't finished spawning?")
                            await event.wait()
                            # channel/portal should now be up
                            _, _, portal = self._children[subactor.uid]
                            if portal is None:
                                # cancelled while waiting on the event?
                                chan = self._actor._peers[subactor.uid][-1]
                                if chan:
                                    portal = Portal(chan)
                                else:  # there's no other choice left
                                    do_hard_kill(proc)

                        # spawn cancel tasks async
                        n.start_soon(portal.cancel_actor)

        log.debug(f"Waiting on all subactors to complete")
        await self.wait()
        self.cancelled = True
        log.debug(f"All subactors for {self} have terminated")

    async def __aexit__(self, etype, value, tb):
        """Wait on all subactor's main routines to complete.
        """
        try:
            if etype is not None:
                # XXX: hypothetically an error could be raised and then
                # a cancel signal shows up slightly after in which case the
                # else block here might not complete? Should both be shielded?
                with trio.open_cancel_scope(shield=True):
                    if etype is trio.Cancelled:
                        log.warn(
                            f"{current_actor().uid} was cancelled with {etype}"
                            ", cancelling actor nursery")
                        await self.cancel()
                    else:
                        log.exception(
                            f"{current_actor().uid} errored with {etype}, "
                            "cancelling actor nursery")
                        await self.cancel()
            else:
                # XXX: this is effectively the lone cancellation/supervisor
                # strategy which exactly mimicks trio's behaviour
                log.debug(f"Waiting on subactors {self._children} to complete")
                try:
                    await self.wait()
                except Exception as err:
                    log.warn(f"Nursery caught {err}, cancelling")
                    await self.cancel()
                    raise
                log.debug(f"Nursery teardown complete")
        except Exception:
            log.exception("Error on nursery exit:")
            await self.wait()
            raise


@asynccontextmanager
async def open_nursery(supervisor=None):
    """Create and yield a new ``ActorNursery``.
    """
    actor = current_actor()
    if not actor:
        raise RuntimeError("No actor instance has been defined yet?")

    # TODO: figure out supervisors from erlang
    async with ActorNursery(current_actor(), supervisor) as nursery:
        yield nursery