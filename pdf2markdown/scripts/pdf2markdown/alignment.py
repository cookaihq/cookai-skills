"""文本规范化（PUA 翻译 + 空白压缩）。

历史上本模块还拥有源块→Markdown 块的 provenance 模糊映射；该机制已于
规则文档架构（基线 §11）中退役，删除判断改由宿主直接在 Markdown 块上
标记。现存函数仅供 manual 流程做文本对比。
"""

import re

_WS = re.compile(r"\s+")
_MD_MARKUP = re.compile(r"[*_`#>|\[\]()!-]")
# live 定型：部分 PDF 内嵌字体把数字映射到私有区 U+F6B1..U+F6BA（0..9），
# 其余私有区码位不可恢复，剥离后再比对。
_PUA_DIGIT_BASE = 0xF6B1
_PUA = re.compile(r"[-]")


def _translate_pua(text):
    out = []
    for ch in text:
        code = ord(ch)
        if _PUA_DIGIT_BASE <= code <= _PUA_DIGIT_BASE + 9:
            out.append(chr(ord("0") + code - _PUA_DIGIT_BASE))
        else:
            out.append(ch)
    return _PUA.sub("", "".join(out))


def normalize_text(text):
    """比对用规范化：翻译/剥离私有区码位、去 Markdown 标记字符、折叠空白。"""
    stripped = _MD_MARKUP.sub(" ", _translate_pua(text))
    return _WS.sub("", stripped).strip()
