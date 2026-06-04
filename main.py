"""nhentai 下载插件 - 基于CDP浏览器和nhentai API"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp

PLUGIN_NAME = "nhentai"


@register(
    PLUGIN_NAME,
    "Bemly",
    "nhentai 搜索下载插件 - 支持搜索、下载nhentai的本子，基于CDP浏览器",
    "1.0.0",
    "https://github.com/bemlyyyyyyyyyyyy/astrbot_plugin_nhentai",
)
class NHentaiPlugin(Star):
    """nhentai 下载插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 数据目录
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir = self.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # CDP配置
        self.cdp_url = self.config.get("cdp_url", "http://127.0.0.1:9222")
        self.cdn_base = self.config.get("cdn_base", "https://zrocdn.xyz")

        logger.info(f"[{PLUGIN_NAME}] 插件初始化完成")

    async def _cdp_navigate(self, ws, url: str, wait: float = 3.0):
        """通过CDP导航到URL"""
        await ws.send(json.dumps({
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": url}
        }))
        await asyncio.wait_for(ws.recv(), timeout=15)
        await asyncio.sleep(wait)

    async def _cdp_evaluate(self, ws, expression: str, msg_id: int = 2):
        """通过CDP执行JavaScript"""
        await ws.send(json.dumps({
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True}
        }))
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        result = json.loads(resp)
        return result.get('result', {}).get('result', {}).get('value', '')

    async def _get_ws_connection(self):
        """获取CDP WebSocket连接"""
        import websockets
        
        resp = httpx.get(f"{self.cdp_url}/json", timeout=5)
        tabs = resp.json()
        if not tabs:
            raise Exception("没有可用的浏览器标签页")
        ws_url = tabs[0]['webSocketDebuggerUrl']
        return await websockets.connect(ws_url, max_size=10*1024*1024)

    async def search_nhentai(self, keyword: str, page: int = 1) -> list[dict]:
        """搜索nhentai"""
        import websockets
        
        results = []
        try:
            ws = await self._get_ws_connection()
            async with ws:
                # 搜索页面
                search_url = f"https://nhentai.to/search/?q={keyword}&page={page}"
                await self._cdp_navigate(ws, search_url, wait=5)
                
                # 获取搜索结果
                js = """
                JSON.stringify(
                    Array.from(document.querySelectorAll('.gallery')).map(g => {
                        const link = g.querySelector('a');
                        const img = g.querySelector('img');
                        const caption = g.querySelector('.caption');
                        return {
                            id: link ? link.href.match(/\\/(\\d+)\\//)?.[1] || '' : '',
                            title: caption ? caption.textContent.trim() : '',
                            cover: img ? (img.dataset.src || img.src) : ''
                        };
                    }).filter(g => g.id)
                )
                """
                data = await self._cdp_evaluate(ws, js)
                results = json.loads(data) if data else []
                
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 搜索失败: {e}")
            
        return results

    async def get_gallery_info(self, gallery_id: str) -> dict | None:
        """获取本子详情"""
        import websockets
        
        try:
            ws = await self._get_ws_connection()
            async with ws:
                url = f"https://nhentai.to/g/{gallery_id}/"
                await self._cdp_navigate(ws, url, wait=5)
                
                # 获取media_id和页数
                js = """
                (function() {
                    var match = document.body.innerHTML.match(/media_id['":\\s]+(\\d+)/);
                    var pages = document.querySelectorAll('.thumb-container').length;
                    var title = document.querySelector('h1') ? document.querySelector('h1').textContent : '';
                    var tags = Array.from(document.querySelectorAll('.tag .name')).map(t => t.textContent);
                    return JSON.stringify({
                        media_id: match ? match[1] : '',
                        pages: pages,
                        title: title,
                        tags: tags.slice(0, 10)
                    });
                })()
                """
                data = await self._cdp_evaluate(ws, js)
                return json.loads(data) if data else None
                
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 获取详情失败: {e}")
            return None

    async def download_gallery(self, gallery_id: str) -> Path | None:
        """下载本子所有图片"""
        info = await self.get_gallery_info(gallery_id)
        if not info or not info.get('media_id'):
            return None
        
        media_id = info['media_id']
        pages = info['pages']
        
        # 创建下载目录
        gallery_dir = self.download_dir / gallery_id
        gallery_dir.mkdir(parents=True, exist_ok=True)
        
        # 下载图片
        downloaded = 0
        async with httpx.AsyncClient(timeout=15) as client:
            for page in range(1, pages + 1):
                for ext in ['webp', 'jpg', 'png']:
                    url = f"{self.cdn_base}/galleries/{media_id}/{page}.{ext}"
                    try:
                        resp = await client.get(url, headers={
                            "Referer": "https://nhentai.to/",
                            "User-Agent": "Mozilla/5.0"
                        }, follow_redirects=True)
                        if resp.status_code == 200 and len(resp.content) > 1000:
                            filepath = gallery_dir / f"{page:03d}.{ext}"
                            filepath.write_bytes(resp.content)
                            downloaded += 1
                            break
                    except:
                        continue
        
        logger.info(f"[{PLUGIN_NAME}] 下载完成: {gallery_id} ({downloaded}/{pages})")
        return gallery_dir if downloaded > 0 else None

    @filter.command("nhs")
    async def search_command(self, event: AstrMessageEvent, keyword: str = None, page: int = 1):
        """搜索nhentai: /nhs <关键词> [页码]"""
        if not keyword:
            yield event.plain_result("用法: /nhs <关键词> [页码]\n示例: /nhs 堕天計画")
            return

        yield event.plain_result(f"正在搜索: {keyword}...")

        results = await self.search_nhentai(keyword, page)
        if not results:
            yield event.plain_result("没有找到结果")
            return

        # 格式化结果
        text = f"nhentai搜索结果 ({keyword}) 第{page}页:\n"
        for i, r in enumerate(results[:10], 1):
            text += f"\n{i}. [{r['id']}] {r['title'][:50]}"
        
        text += f"\n\n共 {len(results)} 个结果"
        text += "\n使用 /nh <ID> 下载本子"
        
        yield event.plain_result(text)

    @filter.command("nh")
    async def download_command(self, event: AstrMessageEvent, gallery_id: str = None):
        """下载nhentai本子: /nh <ID>"""
        if not gallery_id:
            yield event.plain_result("用法: /nh <本子ID>\n示例: /nh 651546")
            return

        gallery_id = str(gallery_id).strip()
        if not gallery_id.isdigit():
            yield event.plain_result("ID必须是数字")
            return

        yield event.plain_result(f"正在下载本子 {gallery_id}...")

        # 获取详情
        info = await self.get_gallery_info(gallery_id)
        if not info:
            yield event.plain_result("获取本子信息失败")
            return

        # 下载
        gallery_dir = await self.download_gallery(gallery_id)
        if not gallery_dir:
            yield event.plain_result("下载失败")
            return

        # 发送图片
        images = sorted(gallery_dir.glob("*.*"))
        title = info.get('title', 'unknown')[:30]
        
        yield event.plain_result(f"下载完成: {title}\n共 {len(images)} 页，开始发送...")

        # 用合并转发发送
        nodes = []
        for img in images:
            node = Comp.Node(
                content=[Comp.Image(file=str(img))],
                name="nhentai",
                user_id="0"
            )
            nodes.append(node)

        forward = Comp.Nodes(nodes=nodes)
        yield event.chain_result([forward])

    @filter.command("nhf")
    async def forward_command(self, event: AstrMessageEvent, gallery_id: str = None):
        """合并转发nhentai本子: /nhf <ID>"""
        if not gallery_id:
            yield event.plain_result("用法: /nhf <本子ID>")
            return

        gallery_id = str(gallery_id).strip()
        yield event.plain_result(f"正在下载并转发: {gallery_id}...")

        # 检查是否已下载
        gallery_dir = self.download_dir / gallery_id
        if not gallery_dir.exists():
            gallery_dir = await self.download_gallery(gallery_id)
            if not gallery_dir:
                yield event.plain_result("下载失败")
                return

        images = sorted(gallery_dir.glob("*.*"))
        
        # 合并转发
        nodes = []
        for img in images:
            node = Comp.Node(
                content=[Comp.Image(file=str(img))],
                name="nhentai",
                user_id="0"
            )
            nodes.append(node)

        forward = Comp.Nodes(nodes=nodes)
        yield event.chain_result([forward])

    @filter.command("nhinfo")
    async def info_command(self, event: AstrMessageEvent, gallery_id: str = None):
        """查看本子信息: /nhinfo <ID>"""
        if not gallery_id:
            yield event.plain_result("用法: /nhinfo <本子ID>")
            return

        info = await self.get_gallery_info(gallery_id)
        if not info:
            yield event.plain_result("获取信息失败")
            return

        text = f"本子信息 [{gallery_id}]:\n"
        text += f"标题: {info.get('title', 'unknown')}\n"
        text += f"页数: {info.get('pages', 0)}\n"
        text += f"Media ID: {info.get('media_id', 'unknown')}\n"
        tags = info.get('tags', [])
        if tags:
            text += f"标签: {', '.join(tags[:10])}"
        
        yield event.plain_result(text)
