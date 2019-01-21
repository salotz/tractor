"""
Messaging pattern APIs and helpers.
"""
import typing
from typing import Dict, Any, Sequence
from functools import partial
from async_generator import aclosing

import trio
import wrapt

from .log import get_logger
from . import current_actor

__all__ = ['pub']

log = get_logger('messaging')


async def fan_out_to_ctxs(
    pub_async_gen_func: typing.Callable,  # it's an async gen ... gd mypy
    topics2ctxs: Dict[str, set],
    packetizer: typing.Callable = None,
) -> None:
    """Request and fan out quotes to each subscribed actor channel.
    """
    def get_topics():
        return tuple(topics2ctxs.keys())

    agen = pub_async_gen_func(get_topics=get_topics)
    async with aclosing(agen) as pub_gen:
        async for published in pub_gen:
            ctx_payloads: Dict[str, Any] = {}
            for topic, data in published.items():
                log.debug(f"publishing {topic, data}")
                # build a new dict packet or invoke provided packetizer
                if packetizer is None:
                    packet = {topic: data}
                else:
                    packet = packetizer(topic, data)
                for ctx in topics2ctxs.get(topic, set()):
                    ctx_payloads.setdefault(ctx, {}).update(packet),

            # deliver to each subscriber (fan out)
            if ctx_payloads:
                for ctx, payload in ctx_payloads.items():
                    try:
                        await ctx.send_yield(payload)
                    except (
                        # That's right, anything you can think of...
                        trio.ClosedStreamError, ConnectionResetError,
                        ConnectionRefusedError,
                    ):
                        log.warning(f"{ctx.chan} went down?")
                        for ctx_set in topics2ctxs.values():
                            ctx_set.discard(ctx)

            if not get_topics():
                log.warning(f"No subscribers left for {pub_gen}")
                break


def modify_subs(topics2ctxs, topics, ctx):
    """Absolute symbol subscription list for each quote stream.

    Effectively a symbol subscription api.
    """
    log.info(f"{ctx.chan.uid} changed subscription to {topics}")

    # update map from each symbol to requesting client's chan
    for ticker in topics:
        topics2ctxs.setdefault(ticker, set()).add(ctx)

    # remove any existing symbol subscriptions if symbol is not
    # found in ``symbols``
    # TODO: this can likely be factored out into the pub-sub api
    for ticker in filter(
        lambda topic: topic not in topics, topics2ctxs.copy()
    ):
        ctx_set = topics2ctxs.get(ticker)
        ctx_set.discard(ctx)

        if not ctx_set:
            # pop empty sets which will trigger bg quoter task termination
            topics2ctxs.pop(ticker)


def pub(
    wrapped: typing.Callable = None,
    *,
    tasks: Sequence[str] = set(),
):
    """Publisher async generator decorator.

    A publisher can be called multiple times from different actors
    but will only spawn a finite set of internal tasks to stream values
    to each caller. The ``tasks` argument to the decorator (``Set[str]``)
    specifies the names of the mutex set of publisher tasks.
    When the publisher function is called, an argument ``task_name`` must be
    passed to specify which task (of the set named in ``tasks``) should be
    used. This allows for using the same publisher with different input
    (arguments) without allowing more concurrent tasks then necessary.

    Values yielded from the decorated async generator
    must be ``Dict[str, Dict[str, Any]]`` where the fist level key is the
    topic string an determines which subscription the packet will be delivered
    to and the value is a packet ``Dict[str, Any]`` by default of the form:

    .. ::python

        {topic: value}

    The caller can instead opt to pass a ``packetizer`` callback who's return
    value will be delivered as the published response.

    The decorated function must *accept* an argument :func:`get_topics` which
    dynamically returns the tuple of current subscriber topics:

    .. code:: python

        from tractor.msg import pub

        @pub(tasks={'source_1', 'source_2'})
        async def pub_service(get_topics):
            data = await web_request(endpoints=get_topics())
            for item in data:
                yield data['key'], data


    The publisher must be called passing in the following arguments:
    - ``topics: Sequence[str]`` the topic sequence or "subscriptions"
    - ``task_name: str`` the task to use (if ``tasks`` was passed)
    - ``ctx: Context`` the tractor context (only needed if calling the
      pub func without a nursery, otherwise this is provided implicitly)
    - packetizer: ``Callable[[str, Any], Any]`` a callback who receives
      the topic and value from the publisher function each ``yield`` such that
      whatever is returned is sent as the published value to subscribers of
      that topic.  By default this is a dict ``{topic: value}``.

    As an example, to make a subscriber call the above function:

    .. code:: python

        from functools import partial
        import tractor

        async with tractor.open_nursery() as n:
            portal = n.run_in_actor(
                'publisher',  # actor name
                partial(      # func to execute in it
                    pub_service,
                    topics=('clicks', 'users'),
                    task_name='source1',
                )
            )
            async for value in portal.result():
                print(f"Subscriber received {value}")


    Here, you don't need to provide the ``ctx`` argument since the remote actor
    provides it automatically to the spawned task. If you were to call
    ``pub_service()`` directly from a wrapping function you would need to
    provide this explicitly.

    Remember you only need this if you need *a finite set of tasks* running in
    a single actor to stream data to an arbitrary number of subscribers. If you
    are ok to have a new task running for every call to ``pub_service()`` then
    probably don't need this.
    """
    # handle the decorator not called with () case
    if wrapped is None:
        return partial(pub, tasks=tasks)

    task2lock = {None: trio.StrictFIFOLock()}
    for name in tasks:
        task2lock[name] = trio.StrictFIFOLock()

    async def takes_ctx(get_topics, ctx=None):
        pass

    @wrapt.decorator(adapter=takes_ctx)
    async def wrapper(agen, instance, args, kwargs):
        task_name = None
        if tasks:
            try:
                task_name = kwargs.pop('task_name')
            except KeyError:
                raise TypeError(
                    f"{agen} must be called with a `task_name` named argument "
                    f"with a falue from {tasks}")

        # pop required kwargs used internally
        ctx = kwargs.pop('ctx')
        topics = kwargs.pop('topics')
        packetizer = kwargs.pop('packetizer', None)

        ss = current_actor().statespace
        lockmap = ss.setdefault('_pubtask2lock', task2lock)
        lock = lockmap[task_name]

        all_subs = ss.setdefault('_subs', {})
        topics2ctxs = all_subs.setdefault(task_name, {})

        try:
            modify_subs(topics2ctxs, topics, ctx)
            # block and let existing feed task deliver
            # stream data until it is cancelled in which case
            # the next waiting task will take over and spawn it again
            async with lock:
                # no data feeder task yet; so start one
                respawn = True
                while respawn:
                    respawn = False
                    log.info(f"Spawning data feed task for {agen.__name__}")
                    try:
                        # unblocks when no more symbols subscriptions exist
                        # and the streamer task terminates
                        await fan_out_to_ctxs(
                            pub_async_gen_func=partial(agen, *args, **kwargs),
                            topics2ctxs=topics2ctxs,
                            packetizer=packetizer,
                        )
                        log.info(f"Terminating stream task {task_name or ''}"
                                 f" for {agen.__name__}")
                    except trio.BrokenResourceError:
                        log.exception("Respawning failed data feed task")
                        respawn = True
        finally:
            # remove all subs for this context
            modify_subs(topics2ctxs, (), ctx)

            # if there are truly no more subscriptions with this broker
            # drop from broker subs dict
            if not any(topics2ctxs.values()):
                log.info(f"No more subscriptions for publisher {task_name}")

    return wrapper(wrapped)