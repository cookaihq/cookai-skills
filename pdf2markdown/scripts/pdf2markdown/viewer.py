"""Local three-column preview, using sanitized build-time Markdown rendering."""
import base64
import hashlib
import html

_ALLOWED_CONTENT_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "thead",
    "tbody", "tr", "td", "th", "code", "pre", "em", "strong", "blockquote", "br",
    "hr", "img", "span", "sup", "sub", "del",
}

_FIXED_SCRIPT = (
    "document.querySelectorAll('[data-toggle]').forEach(function(btn){"
    "btn.addEventListener('click',function(){"
    "var col=document.getElementById(btn.getAttribute('data-toggle'));"
    "col.classList.toggle('hidden');});});"
)

_FIXED_STYLE = (
    "html,body{height:100%;}"
    "body{margin:0;font-family:sans-serif;}nav{height:40px;display:flex;gap:8px;align-items:center;padding:0 12px;} .col{padding:12px;}"
    # >=1024px：.cols 占满视口高，三列各自独立滚动，页面本身不滚。
    ".cols{display:flex;gap:8px;height:calc(100vh - 40px);}"
    ".col{flex:1 1 0;min-width:0;height:100%;overflow-y:auto;"
    "overflow-wrap:anywhere;box-sizing:border-box;}"
    ".col.hidden{display:none;}"
    ".colhead{display:flex;align-items:center;gap:8px;}"
    # <1024px：竖排恢复自然高度与整页滚动（§14.3 移动布局）。
    "@media (max-width:1023px){.cols{flex-direction:column;height:auto;}"
    ".col{height:auto;overflow-y:visible;}}"
    "img{max-width:100%;}"
    "table{display:block;max-width:100%;overflow-x:auto;}"
    "pre{white-space:pre-wrap;overflow-wrap:anywhere;}"
    "object{width:100%;height:80vh;}"
)


class ViewerError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def _render_markdown(markdown_bytes, pandoc_path, allowed_image_urls):
    """pandoc 构建期渲染 + nh3 内容 allowlist 清理。"""
    import subprocess

    import nh3

    proc = subprocess.run(
        [pandoc_path, "-f", "gfm", "-t", "html"],
        input=markdown_bytes,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise ViewerError("viewer_input_invalid", "markdown failed structured parsing")
    raw_html = proc.stdout.decode("utf-8")

    def attribute_filter(tag, attribute, value):
        if tag == "img" and attribute == "src":
            return value if value in allowed_image_urls else None
        if attribute in ("alt", "title"):
            return value
        return None

    cleaned = nh3.clean(
        raw_html,
        tags=_ALLOWED_CONTENT_TAGS,
        attributes={"img": {"src", "alt", "title"}, "span": set(), "td": set(), "th": set()},
        attribute_filter=attribute_filter,
        link_rel=None,
        strip_comments=True,
    )
    return cleaned



def render(title, direct, final, pandoc, image_paths):
    direct_html = _render_markdown(direct, pandoc, set(image_paths))
    final_html = _render_markdown(final, pandoc, set(image_paths))
    script_hash = base64.b64encode(hashlib.sha256(_FIXED_SCRIPT.encode()).digest()).decode()
    style_hash = base64.b64encode(hashlib.sha256(_FIXED_STYLE.encode()).digest()).decode()
    csp = ("default-src 'none'; img-src 'self' file:; object-src 'self' file:; "
           "frame-src 'self' file:; base-uri 'none'; form-action 'none'; "
           "style-src 'sha256-%s'; script-src 'sha256-%s'") % (style_hash, script_hash)
    controls = '<nav aria-label="显示或隐藏列">' + ''.join(
        '<button type="button" data-toggle="%s">%s</button>' % (ident, name)
        for ident, name in [('col-pdf', 'PDF 原文'), ('col-direct', '直接转换结果'), ('col-final', '最终处理结果')]
    ) + '</nav>'
    def column(ident, name, filename, content):
        return ('<section class="col" id="%s"><div class="colhead"><h2>%s</h2>'
                '<a download="%s" href="%s">下载</a></div>%s</section>') % (
                    ident, name, filename, filename, content)
    pdf = '<object type="application/pdf" data="source.pdf"><p>请下载 source.pdf 查看原文。</p></object>'
    document = ('<!doctype html><html lang="zh"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<meta http-equiv="Content-Security-Policy" content="%s">'
                '<title>%s</title><style>%s</style></head><body>%s<div class="cols">%s%s%s</div>'
                '<script>%s</script></body></html>') % (
        html.escape(csp, quote=True), html.escape(title), _FIXED_STYLE, controls,
        column('col-pdf', 'PDF 原文', 'source.pdf', pdf),
        column('col-direct', '直接转换结果', 'direct.md', direct_html),
        column('col-final', '最终处理结果', 'final.md', final_html), _FIXED_SCRIPT)
    return document.encode('utf-8')
