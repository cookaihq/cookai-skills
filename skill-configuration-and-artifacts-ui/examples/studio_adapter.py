"""Synthetic data only. Copy and adapt outside the vendored core after display approval."""
import io
from pathlib import Path
import struct
import wave

from skill_ui import Adapter, Resource, UIError
from skill_ui.storage import JsonConfig

TEXT = "# 山间的一天\n\n一次关于自然、工作与生活节奏的访谈。\n\n## 内容摘要\n\n留出一点空白，让重要的事情有机会发生。\n\n## 关键片段\n\n00:18 清晨的山路：观察与记录。\n01:24 工作与休息：安排专注时间。\n\n此文档为合成示例。"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="600" viewBox="0 0 960 600"><rect width="960" height="600" fill="#dce6d5"/><circle cx="720" cy="140" r="55" fill="#f8ebc6"/><path d="M0 500L240 130 510 470 700 210 960 500V600H0Z" fill="#a9c2aa"/><path d="M0 580L220 320 520 540 720 350 960 560V600H0Z" fill="#73967c"/><path d="M0 580Q240 440 500 560T960 490V600H0Z" fill="#476d53"/></svg>'


class Studio(Adapter):
    title = "内容工作室 · 示例 Plugin"

    def __init__(self, project):
        self.project = project
        self.store = JsonConfig(project / "demo-state" / "settings.json", [
            {"id":"api_key","label":"API Key","type":"text","sensitive":True,"required":True,"group":"连接设置",
             "help":"只在本演示中使用合成值，例如 demo-key-123456。","clear_help":"清除后需要重新填写。"},
            {"id":"endpoint","label":"API 地址","type":"text","default":"https://example.invalid/api","group":"连接设置",
             "effect":"下次任务生效；示例不会发送业务网络请求。"},
            {"id":"format","label":"文档格式","type":"select","choices":["Markdown","纯文本"],"default":"Markdown","group":"文档设置"},
            {"id":"retain","label":"保留任务记录","type":"boolean","default":True,"group":"产物设置"},
            {"id":"days","label":"保留天数","type":"number","default":30,"depends_on":{"retain":True},"group":"产物设置","advanced":True},
            {"id":"priority","label":"语言优先级","type":"list","default":["中文","English"],"group":"文档设置","advanced":True},
            {"id":"workspace","label":"项目绑定","type":"text","default":str(project),"readonly":True,"group":"项目"},
        ], owner_path=project)
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(b"".join(struct.pack("<h", 1600 if i%24<12 else -1600) for i in range(16000)))
        self.resources = {
            "document": Resource(TEXT.encode(), "text/plain; charset=utf-8", "访谈文稿.md"),
            "cover": Resource(SVG.encode(), "image/svg+xml", "山间的一天.svg"),
            "audio": Resource(audio.getvalue(), "audio/wav", "提示音.wav"),
            "table": Resource("名称,状态\n访谈文稿,可用\n封面图片,可用\n".encode("utf-8-sig"), "text/csv; charset=utf-8", "成果清单.csv"),
        }

    def config(self):
        return dict(self.store.snapshot(), note="合成演示：这里的保存仅写入所指定演示项目，不调用外部服务。",
                    presentation={"title":"配置内容工作室", "focus":["format", "retain"]},
                    task={"id":"demo-interview-001","binding":str(self.project),
                          "continuation":"继续当前演示项目的 demo-interview-001 任务；先核对配置和原任务授权。"})

    def preview(self, changes, revision, target):
        return self.store.preview(changes, revision, target)

    def save(self, changes, revision, target):
        self.store.save(changes, revision, target)
        return self.config()

    def artifacts(self):
        return {"note":"合成示例 · 三项任务的文稿、媒体与操作结果", "items":[
            {"id":"document","title":"访谈文稿","kind":"document","task":"山间访谈","resource":"document","summary":"Markdown · 已整理的访谈内容","preview":{"type":"markdown","text":TEXT},"metadata":{"格式":"Markdown","任务":"山间访谈","数据":"合成示例"}},
            {"id":"cover","title":"山间的一天","kind":"image","task":"山间访谈","resource":"cover","summary":"SVG · 960 × 600","preview":{"type":"image","resource":"cover"}},
            {"id":"audio","title":"示例提示音","kind":"audio","task":"山间访谈","resource":"audio","summary":"WAV · 2 秒","preview":{"type":"audio","resource":"audio"}},
            {"id":"table","title":"产物清单","kind":"table","task":"成果整理","resource":"table","summary":"CSV · 两条成果记录","preview":{"type":"table","columns":["名称","类型","状态"],"rows":[["访谈文稿","Markdown","可用"],["封面图片","SVG","可用"]]}},
            {"id":"receipt","title":"远端上传回执","kind":"receipt","task":"成果整理","status":"链接已过期","summary":"操作记录保留；没有自动重新上传。","preview":{"type":"custom","renderer":"receipt","data":{"status":"链接已过期","reference":"example://synthetic/upload-001","message":"此项是合成回执，不对应真实远端资源。"}},"copy_text":"example://synthetic/upload-001"},
            {"id":"comparison","title":"文稿修订对照","kind":"comparison","task":"文稿校对","summary":"直接稿与最终稿","preview":{"type":"comparison","panels":[{"title":"直接稿","text":"# 山间的一天\n\n观察与记录。"},{"title":"最终稿","text":TEXT}]}}
        ]}

    def resource(self, resource_id):
        if resource_id not in self.resources:
            raise UIError("missing", "此产物文件不存在。", 404)
        return self.resources[resource_id]

    def extensions(self):
        return {"receipt-view.js": Path(__file__).with_name("receipt-view.js")}


def build(project):
    return Studio(project)
