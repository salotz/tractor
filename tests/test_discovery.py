"""
Actor "discovery" testing
"""
import tractor
import trio

from conftest import tractor_test


@tractor_test
async def test_reg_then_unreg(arb_addr):
    actor = tractor.current_actor()
    assert actor.is_arbiter
    assert len(actor._registry) == 1  # only self is registered

    async with tractor.open_nursery() as n:
        portal = await n.start_actor('actor', rpc_module_paths=[__name__])
        uid = portal.channel.uid

        async with tractor.get_arbiter(*arb_addr) as aportal:
            # local actor should be the arbiter
            assert actor is aportal.actor

            # sub-actor uid should be in the registry
            await trio.sleep(0.1)  # registering is async, so..
            assert uid in aportal.actor._registry
            sockaddrs = actor._registry[uid]
            # XXX: can we figure out what the listen addr will be?
            assert sockaddrs

        await n.cancel()  # tear down nursery

        await trio.sleep(0.1)
        assert uid not in aportal.actor._registry
        sockaddrs = actor._registry[uid]
        assert not sockaddrs


the_line = 'Hi my name is {}'


async def hi():
    return the_line.format(tractor.current_actor().name)


async def say_hello(other_actor):
    await trio.sleep(0.4)  # wait for other actor to spawn
    async with tractor.find_actor(other_actor) as portal:
        return await portal.run(__name__, 'hi')


@tractor_test
async def test_trynamic_trio():
    """Main tractor entry point, the "master" process (for now
    acts as the "director").
    """
    async with tractor.open_nursery() as n:
        print("Alright... Action!")

        donny = await n.run_in_actor(
            'donny',
            say_hello,
            other_actor='gretchen',
        )
        gretchen = await n.run_in_actor(
            'gretchen',
            say_hello,
            other_actor='donny',
        )
        print(await gretchen.result())
        print(await donny.result())
        await donny.cancel_actor()
        print("CUTTTT CUUTT CUT!!?! Donny!! You're supposed to say...")
