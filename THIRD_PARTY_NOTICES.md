# 来源及第三方许可

本项目仅按符号提取 douyin-mcp 的收藏读取能力，不包含原服务端及无关工具。

- 来源：https://github.com/pazwusimple-netizen/douyin-mcp
- 基线：ab9eb3b；提取时包含本机现有收藏及 SecSDK 增强的工作区版本。
- 提取：client.py 的请求参数、签名请求、收藏分页及三个读取方法；cookies.py 的规范化；models.py 的校验参数及图文 URL 解析；server.py 的收藏作品字段整理；sign.py 的运行环境；token_manager.py 的必要校验流程。
- JavaScript 仅保留签名 VM 与 get_abogus 入口，删除原环境补丁及演示代码、样例查询参数。
- MIT 许可见 LICENSE。没有复制登录凭据、浏览器状态、媒体、原依赖锁文件或原 MCP 注册逻辑。

# Collection signing protocol reference

`src/collection_sign.py` implements the SecSDK query-signing protocol documented
in `ucmao/media-parser`, commit `36e57ff489d23e1037050c9de7f0a39598f8caee`,
`src/parsers/douyin_parser.py` (`_sign_secsdk`).

Source: https://github.com/ucmao/media-parser/blob/36e57ff489d23e1037050c9de7f0a39598f8caee/src/parsers/douyin_parser.py

The reference project is licensed as follows:

MIT License

Copyright (c) 2025-2026 ucmao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


# 收藏夹移动协议参考

移动与夹内移出参数参考 cv-cat/DouYin_Spider 的 move_collect_aweme、remove_collect_aweme，仅按协议构造请求，未复制其完整项目或签名/登录框架。
来源：https://github.com/cv-cat/DouYin_Spider/blob/master/dy_apis/douyin_api.py

# 字体与示例素材

- DM Sans 来自 Google Fonts，SIL Open Font License 1.1，许可保存在 web/assets/DM-Sans-OFL.txt。
- web/assets/demo-sheet.png 由内置 image_gen 生成，仅在明确标识的示例模式使用，不代表真实账号收藏。
- 生成提示词：六格、三列两行、无文字无边框的自然摄影拼图，依次为笔记本办公桌、阳光客厅、番茄牛腩、窗边湖景、窗前拉伸、自动化工作台；柔和日光、真实材质和统一低饱和色调。以网页 CSS 精确定位各格作为示例封面。
