"""Execute the browser observation guard without allocating a huge real DOM."""
import json
import unittest

from vibe_job_radar.guided.rendered_dom import CAPTURE, capture
from vibe_job_radar.guided.contracts import CrawlError


class RenderedObservationBoundsTests(unittest.TestCase):
    def test_node_budget_rejects_before_serialization_clone_or_computed_styles(self):
        import quickjs
        context = quickjs.Context()
        context.eval("""
            let visits = 0;
            const root = { get outerHTML() { throw Error("serialized too early"); } };
            const NodeFilter = { SHOW_ALL: 4294967295, SHOW_ELEMENT: 1 };
            const document = { documentElement: root,
                createTreeWalker(node, mask) { return { nextNode() { visits++; return mask === 4294967295 ? {} : null; } }; },
                get implementation() { throw Error("cloned too early"); } };
        """)
        class Page:
            def evaluate(self, script):
                return json.loads(context.eval("JSON.stringify((" + script + ")())"))
        with self.assertRaises(CrawlError) as caught:
            capture(Page(), "https://example.com/job")
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertLessEqual(context.eval("visits"), 20001)
        self.assertGreater(context.eval("visits"), 0)

    def test_bounded_dom_captures_original_and_computed_copy(self):
        import quickjs
        context = quickjs.Context()
        context.eval("""
            const source = "<html><body>original</body></html>";
            const rendered = "<html style='display:block'>observed</html>";
            const style = {removeProperty(){}, setProperty(){}};
            const copy = {tagName:"HTML", style, removeAttribute(){},
                          querySelectorAll(){return []}, outerHTML:rendered};
            const root = {tagName:"HTML", outerHTML:source, querySelectorAll(){return []}};
            const NodeFilter = {SHOW_ALL:4294967295, SHOW_ELEMENT:1};
            const document = {documentElement:root, doctype:null, body:{innerText:"original"},
                createTreeWalker(){return {nextNode(){return null;}}},
                implementation:{createHTMLDocument(){return {importNode(){return copy;}}}}};
            const location = {href:"https://example.com/job"};
            function getComputedStyle(){return {contentVisibility:"visible",
                                                getPropertyValue(){return "block";}};}
        """)
        class Page:
            def evaluate(self, script):
                return json.loads(context.eval("JSON.stringify((" + script + ")())"))
        original, text, rendered = capture(Page(), "https://example.com/job")
        self.assertEqual(original, "<html><body>original</body></html>")
        self.assertEqual(text, "original")
        self.assertIn("observed", rendered)
