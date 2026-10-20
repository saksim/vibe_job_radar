"""Read-only browser style observation; never replace original HTML evidence."""
from .contracts import CrawlError, PageSnapshotChanged

# Import into an inert document: custom element constructors and resource loads
# must not run merely because we serialize a style observation. Never attach it.
CAPTURE = """() => {
    const root = document.documentElement;
    if (!root) return null;
    const source = root.outerHTML;
    const doctype = document.doctype ? new XMLSerializer().serializeToString(document.doctype) : "";
    const text = document.body ? document.body.innerText : "";
    if (source.length > 5000000) return null;
    const inert = document.implementation.createHTMLDocument("");
    const copy = inert.importNode(root, true);
    const originalNodes = [root, ...root.querySelectorAll("*")];
    const copiedNodes = [copy, ...copy.querySelectorAll("*")];
    for (let index = 0; index < originalNodes.length; index++) {
        const style = getComputedStyle(originalNodes[index]);
        const target = copiedNodes[index];
        if (!target || target.tagName !== originalNodes[index].tagName) return null;
        // Hidden's actual CSS effect is captured below, including until-found
        // and author overrides. Only the inert observation loses the attribute.
        target.removeAttribute("hidden");
        for (const name of ["display", "visibility", "position", "float"]) {
            target.style.setProperty(name, style.getPropertyValue(name), "important");
        }
        if (style.contentVisibility === "hidden") {
            target.style.setProperty("display", "none", "important");
        }
    }
    const rendered = copy.outerHTML;
    if (rendered.length > 5000000) return null;
    return {source, doctype, text, rendered};
}"""


def capture(page, original, text):
    value = page.evaluate(CAPTURE)
    if not isinstance(value, dict) or any(
            not isinstance(value.get(k), str) or len(value[k]) > 5_000_000
            for k in ('source', 'doctype', 'text', 'rendered')):
        raise CrawlError('invalid_page_observation')
    # Playwright includes the doctype; the minimal CDP backend returns outerHTML.
    if original not in (value['source'], value['doctype'] + value['source']) or text != value['text']:
        raise PageSnapshotChanged()
    return value['rendered']
