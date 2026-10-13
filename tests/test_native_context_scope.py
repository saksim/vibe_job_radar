"""Browser target ownership must not confuse bootstrap and collection contexts."""
from unittest import TestCase
from unittest.mock import Mock

import test_native_acquisition as fixture


class NativeContextScopeTests(TestCase):
    def setUp(self):
        helper = fixture.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.b = helper.b

    def test_default_context_bootstrap_target_does_not_cancel_collection(self):
        self.b._context_id='collection-context'
        self.b._attached({'sessionId':'bootstrap','targetInfo':{
            'type':'page','targetId':'bootstrap','url':'about:blank'}})
        self.b._cdp.send.assert_called_once_with('Target.detachFromTarget',{'sessionId':'bootstrap'})
        self.assertFalse(self.b.cancelled.is_set())
        self.assertIsNone(self.b.error)

    def test_foreign_context_never_gains_our_page_controls(self):
        self.b._context_id='collection-context'
        self.b._install_target=Mock()
        self.b._attached({'sessionId':'foreign','targetInfo':{
            'type':'page','targetId':'foreign','browserContextId':'other'}})
        self.b._install_target.assert_not_called()
        self.b._cdp.send.assert_called_once_with('Target.detachFromTarget',{'sessionId':'foreign'})

    def test_popup_in_collection_context_still_stops_and_is_not_resumed(self):
        self.b._context_id='collection-context'
        self.b._cdp.send.return_value={'sessionId':'quarantine'}
        self.b._attached({'sessionId':'popup','targetInfo':{
            'type':'page','targetId':'popup','browserContextId':'collection-context','openerId':'owned'}})
        self.b._cdp.send.assert_called_once_with('Target.attachToTarget', {
            'targetId':'popup', 'flatten':False})
        self.b._send.assert_called_once_with('quarantine', 'Fetch.enable', {
            'patterns':[{'urlPattern':'*', 'requestStage':'Request'}],
            'handleAuthRequests':True})
        self.assertTrue(self.b.cancelled.is_set())
        self.assertTrue(self.b._halted)
        self.assertEqual(self.b._pending_rejected_targets,['popup'])
