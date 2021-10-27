"""
Testing scenarios that may have leaked.
"""
from __future__ import print_function, absolute_import, division

import sys
import gc

import time
import weakref
import threading

import greenlet
from . import TestCase
from .leakcheck import fails_leakcheck

try:
    from sys import intern
except ImportError:
    # Python 2
    pass

assert greenlet.GREENLET_USE_GC # Option to disable this was removed in 1.0

class HasFinalizerTracksInstances(object):
    EXTANT_INSTANCES = set()
    def __init__(self, msg):
        self.msg = intern(msg)
        self.EXTANT_INSTANCES.add(id(self))
    def __del__(self):
        self.EXTANT_INSTANCES.remove(id(self))
    def __repr__(self):
        return "<HasFinalizerTracksInstances at 0x%x %r>" % (
            id(self), self.msg
        )
    @classmethod
    def reset(cls):
        cls.EXTANT_INSTANCES.clear()


class TestLeaks(TestCase):

    def test_arg_refs(self):
        args = ('a', 'b', 'c')
        refcount_before = sys.getrefcount(args)
        # pylint:disable=unnecessary-lambda
        g = greenlet.greenlet(
            lambda *args: greenlet.getcurrent().parent.switch(*args))
        for _ in range(100):
            g.switch(*args)
        self.assertEqual(sys.getrefcount(args), refcount_before)

    def test_kwarg_refs(self):
        kwargs = {}
        # pylint:disable=unnecessary-lambda
        g = greenlet.greenlet(
            lambda **kwargs: greenlet.getcurrent().parent.switch(**kwargs))
        for _ in range(100):
            g.switch(**kwargs)
        self.assertEqual(sys.getrefcount(kwargs), 2)


    @staticmethod
    def __recycle_threads():
        # By introducing a thread that does sleep we allow other threads,
        # that have triggered their __block condition, but did not have a
        # chance to deallocate their thread state yet, to finally do so.
        # The way it works is by requiring a GIL switch (different thread),
        # which does a GIL release (sleep), which might do a GIL switch
        # to finished threads and allow them to clean up.
        def worker():
            time.sleep(0.001)
        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.001)
        t.join(10)

    def test_threaded_leak(self):
        gg = []
        def worker():
            # only main greenlet present
            gg.append(weakref.ref(greenlet.getcurrent()))
        for _ in range(2):
            t = threading.Thread(target=worker)
            t.start()
            t.join(10)
            del t
        greenlet.getcurrent() # update ts_current
        self.__recycle_threads()
        greenlet.getcurrent() # update ts_current
        gc.collect()
        greenlet.getcurrent() # update ts_current
        for g in gg:
            self.assertIsNone(g())

    def test_threaded_adv_leak(self):
        gg = []
        def worker():
            # main and additional *finished* greenlets
            ll = greenlet.getcurrent().ll = []
            def additional():
                ll.append(greenlet.getcurrent())
            for _ in range(2):
                greenlet.greenlet(additional).switch()
            gg.append(weakref.ref(greenlet.getcurrent()))
        for _ in range(2):
            t = threading.Thread(target=worker)
            t.start()
            t.join(10)
            del t
        greenlet.getcurrent() # update ts_current
        self.__recycle_threads()
        greenlet.getcurrent() # update ts_current
        gc.collect()
        greenlet.getcurrent() # update ts_current
        for g in gg:
            self.assertIsNone(g())

    def _check_issue251(self,
                        manually_collect_background=True,
                        explicit_reference_to_switch=False):
        # See https://github.com/python-greenlet/greenlet/issues/251
        # Killing a greenlet (probably not the main one)
        # in one thread from another thread would
        # result in leaking a list (the ts_delkey list).
        # We no longer use lists to hold that stuff, though.

        # For the test to be valid, even empty lists have to be tracked by the
        # GC

        assert gc.is_tracked([])
        HasFinalizerTracksInstances.reset()
        greenlet.getcurrent()
        greenlets_before = self.count_objects(greenlet.greenlet, exact_kind=False)

        background_glet_running = threading.Event()
        background_glet_killed = threading.Event()
        background_greenlets = []

        # XXX: Switching this to a greenlet subclass that overrides
        # run results in all callers failing the leaktest; that
        # greenlet instance is leaked. There's a bound method for
        # run() living on the stack of the greenlet in g_initialstub,
        # and since we don't manually switch back to the background
        # greenlet to let it "fall off the end" and exit the
        # g_initialstub function, it never gets cleaned up. Making the
        # garbage collector aware of this bound method (making it an
        # attribute of the greenlet structure and traversing into it)
        # doesn't help, for some reason.
        def background_greenlet():
            # Throw control back to the main greenlet.
            jd = HasFinalizerTracksInstances("DELETING STACK OBJECT")
            greenlet._greenlet.set_thread_local(
                'test_leaks_key',
                HasFinalizerTracksInstances("DELETING THREAD STATE"))
            # Explicitly keeping 'switch' in a local variable
            # breaks this test in all versions
            if explicit_reference_to_switch:
                s = greenlet.getcurrent().parent.switch
                s([jd])
            else:
                greenlet.getcurrent().parent.switch([jd])

        bg_main_wrefs = []

        def background_thread():
            glet = greenlet.greenlet(background_greenlet)
            bg_main_wrefs.append(weakref.ref(glet.parent))

            background_greenlets.append(glet)
            glet.switch() # Be sure it's active.
            # Control is ours again.
            del glet # Delete one reference from the thread it runs in.
            background_glet_running.set()
            background_glet_killed.wait(10)

            # To trigger the background collection of the dead
            # greenlet, thus clearing out the contents of the list, we
            # need to run some APIs. See issue 252.
            if manually_collect_background:
                greenlet.getcurrent()


        t = threading.Thread(target=background_thread)
        t.start()
        background_glet_running.wait(10)
        greenlet.getcurrent()
        lists_before = self.count_objects(list, exact_kind=True)

        assert len(background_greenlets) == 1
        self.assertFalse(background_greenlets[0].dead)
        # Delete the last reference to the background greenlet
        # from a different thread. This puts it in the background thread's
        # ts_delkey list.
        del background_greenlets[:]
        background_glet_killed.set()

        # Now wait for the background thread to die.
        t.join(10)
        del t
        # As part of the fix for 252, we need to cycle the ceval.c
        # interpreter loop to be sure it has had a chance to process
        # the pending call.
        self.wait_for_pending_cleanups()

        lists_after = self.count_objects(list, exact_kind=True)
        greenlets_after = self.count_objects(greenlet.greenlet, exact_kind=False)

        # On 2.7, we observe that lists_after is smaller than
        # lists_before. No idea what lists got cleaned up. All the
        # Python 3 versions match exactly.
        self.assertLessEqual(lists_after, lists_before)
        # On versions after 3.6, we've successfully cleaned up the
        # greenlet references thanks to the internal "vectorcall"
        # protocol; prior to that, there is a reference path through
        # the ``greenlet.switch`` method still on the stack that we
        # can't reach to clean up. The C code goes through terrific
        # lengths to clean that up.
        if not explicit_reference_to_switch:
            self.assertEqual(greenlets_after, greenlets_before)
            if manually_collect_background:
                # TODO: Figure out how to make this work!
                # The one on the stack is still leaking somehow
                # in the non-manually-collect state.
                self.assertEqual(HasFinalizerTracksInstances.EXTANT_INSTANCES, set())
        else:
            # The explicit reference prevents us from collecting it
            # and it isn't always found by the GC either for some
            # reason. The entire frame is leaked somehow, on some
            # platforms (e.g., MacPorts builds of Python (all
            # versions!)), but not on other platforms (the linux and
            # windows builds on GitHub actions and Appveyor). So we'd
            # like to write a test that proves that the main greenlet
            # sticks around, and we can on my machine (macOS 11.6,
            # MacPorts builds of everything) but we can't write that
            # same test on other platforms. However, hopefully iteration
            # done by leakcheck will find it.
            pass

    def test_issue251_killing_cross_thread_leaks_list(self):
        self._check_issue251()

    @fails_leakcheck
    def test_issue251_issue252_need_to_collect_in_background(self):
        # Between greenlet 1.1.2 and the next version, this was still
        # failing because the leak of the list still exists when we
        # don't call a greenlet API before exiting the thread. The
        # proximate cause is that neither of the two greenlets from
        # the background thread are actually being destroyed, even
        # though the GC is in fact visiting both objects. It's not
        # clear where that leak is? For some reason the thread-local
        # dict holding it isn't being cleaned up.
        #
        # The leak, I think, is in the CPYthon internal function that
        # calls into green_switch(). The argument tuple is still on
        # the C stack somewhere and can't be reached? That doesn't
        # make sense, because the tuple should be collectable when
        # this object goes away.
        #
        # Note that this test sometimes spuriously passes on Linux,
        # for some reason, but I've never seen it pass on macOS.
        self._check_issue251(manually_collect_background=False)

    @fails_leakcheck
    def test_issue251_issue252_explicit_reference_not_collectable(self):
        self._check_issue251(
            manually_collect_background=False,
            explicit_reference_to_switch=True)
