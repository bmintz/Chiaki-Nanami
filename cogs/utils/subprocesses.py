import asyncio
import subprocess


async def run_subprocess(cmd, loop=None):
    loop = loop or asyncio.get_event_loop()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    # XXX: On platforms that have event loops that don't support subprocesses,
    #      the NotImplementedError exception will always be tripped. From what
    #      I've heard always raising an exception and triggering the except
    #      statement it up to 10-20x slower. But there's no LBYL or one-shot
    #      way of checking whether or not a given even loop supports subprocesses.
    except NotImplementedError:
        # The default event loop for Windows doesn't support subprocesses. To
        # make matters worse, we can't use the proactor event loop because
        # there is a regression in Python 3.6 where an AssertionError is
        # thrown when reading the data.
        #
        # This is why we have no choice but to resort to using an executor.
        # If anyone can supply a minimal repro of this issue and submit it
        # to the Python bug tracker that would be really nice (I can't sadly
        # because I'm not smart enough to figure out what's going on).
        #
        # See: https://github.com/Rapptz/discord.py/issues/859
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        ) as proc:
            try:
                result = await loop.run_in_executor(None, proc.communicate)
            except:
                def kill():
                    proc.kill()
                    proc.wait()
                # Wait for the process to die but don't block the loop.
                await loop.run_in_executor(None, kill)
                raise
    else:
        result = await proc.communicate()

    return [x.decode('utf-8') for x in result]
