"""Read-only browser style observation; never replace original HTML evidence."""
from .contracts import CrawlError, PageSnapshotChanged

# Import into an inert document: custom element constructors and resource loads
# must not run merely because we serialize a style observation. Never attach it.
CAPTURE = """() => {
    const root = document.documentElement;
    if (!root) return null;
    // Count before serialization, cloning, arrays, innerText or style/layout
    // reads. A byte limit alone admits hundreds of thousands of tiny nodes.
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let elements = 1;
    while (walker.nextNode()) {
        if (++elements > 20000) return {observation_limit: "elements"};
    }
    const source = root.outerHTML;
    if (source.length > 5000000) return null;
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
        target.style.removeProperty("all");
        for (const name of ["display", "visibility", "position", "float"]) {
            target.style.setProperty(name, style.getPropertyValue(name), "important");
        }
        if (style.contentVisibility === "hidden") {
            target.style.setProperty("display", "none", "important");
        }
    }
    const rendered = copy.outerHTML;
    if (rendered.length > 5000000) return null;
    return {source, doctype, text, rendered, url: location.href};
}"""


def capture(page, expected_url):
    value = page.evaluate(CAPTURE)
    if isinstance(value, dict) and value.get('observation_limit') == 'elements':
        raise CrawlError('response_too_large')
    if not isinstance(value, dict) or any(
            not isinstance(value.get(k), str) or len(value[k]) > 5_000_000
            for k in ('source', 'doctype', 'text', 'rendered', 'url')):
        raise CrawlError('invalid_page_observation')
    if value['url'] != expected_url:
        raise PageSnapshotChanged()
    original = value['doctype'] + value['source']
    if len(original) > 5_000_000:
        raise CrawlError('response_too_large')
    # One synchronous observation supplies all three. Comparing it to an
    # earlier body/content call rejects normal in-page updates, especially
    # the login-return search form. Never combine bytes from different reads.
    return original, value['text'], value['rendered']
