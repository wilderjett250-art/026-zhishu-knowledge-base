# Everything 接入及先发现后分类

## 最新复验 · 2026-09-09 22:30

- 用户再次明确授权重启后，Everything原命令执行成功。reports/everything-real-check.py使用新建合成目录，3/3文件正确返回，node_modules排除，中文/空格/逗号路径通过，原件未变，EFU临时文件正常清理。小样本0.234秒，不代表全盘速度。
- reports/everything-real-ui-check.cjs在隔离8879后端真实走Everything→分类→策略调整→确认→全文搜索命中1条；截图everything-real-ui-20260909.png。这次不再是假进程/native替代测试。
- 主8765API重启仍被工具执行策略拒绝，未执行停止或启动。当前旧服务尚未加载新接口，不能称正式EXE后端已经更新完成；需要本机正常重启该后台后复验。
- 只读检查确认原正式API仍在；没有换工具/其他通道绕过重启拒绝，没有更改全局策略或启用自启动。
- 本轮恢复点I:/PKAS-backups/everything-restart-20260909，SQLite在线备份quick_check=ok；本文件/HANDOFF/PROVENANCE前态逐文件hash一致。

## 扫描职责的设计结论

| 场景 | 决策 |
|---|---|
| 用户新接入本地目录、列出文件并做类型分类 | 默认Everything文件发现器；当前已真实验证 |
| Everything不可用或不支持的环境 | 用户明确选择普通扫描，保留兼容通道，不偷偷换引擎 |
| Word/PDF/Excel/聊天正文提取 | 保留专用解析模块，Everything文件名索引不替代内容读取 |
| 检查资料修改、去重、索引一致性 | 保留指纹/hash/数据库校验，不依据路径清单直接认定内容未变化 |
| 微信导出、QQ连接器、向量任务 | 保留原模块，不是本地文件发现器的职责 |
| 旧SyncService全量台账扫描 | 本轮不迁移、不启动；它含missing标记逻辑，换扫描器需先验证完整性，防止遗漏被误判丢失 |

不把“所有扫描都换Everything”作为目标。文件发现统一、内容处理分工，才符合个人知识库用途。当前没有MFT/USN常驻索引；是否引入需要实测和确认盘范围，不能凭3文件测试保证500GB效果。

以下为上一轮记录，Everything执行受阻部分已被上面的真实复验取代；正式后台重启受阻仍有效。

## 已实现

- 官方免费标准便携版1.4.1.1032 x64位于tools/everything，保留官方License.txt与PROVENANCE.md；ZIP SHA256与官网一致。
- Everything适配层通过-create-file-list导出所选目录元信息，不安装服务/自启动，不启动常驻MFT索引，未承诺性能倍数。
- 新EXE页面：选目录→发现文件→自动按扩展名分五类并统计数量/大小→调整处理方式→更新预览→确认入库。扫描前不要求设置分类规则。
- 扫描后改方案不重扫，只有未执行批次可改；默认Everything，失败明确报告，不静默降级；用户可手动选普通扫描。
- Everything最长120秒/清单64MB；候选默认2000、API上限10000，超限阻止确认。仍不是500GB整盘完整处理方案。
- 校验返回路径范围、链接/重解析点、敏感/系统/缓存排除；保持原件引用和既有确认/备份门禁。内容业务含义分类未实现。

## 验证与实际阻塞

- 32 passed, 1 skipped；Everything扫描器用合成EFU/假子进程测试，包含参数边界、取消、不静默降级；symlink权限用例跳过。
- Ruff、Web TypeScript/Vite build通过。
- reports/discover-first-ui-check.cjs：真实隔离HTTP页面用native验证扫描前0规则、扫描后5类、改方案/确认；截图discover-first-native-ui-20260909.png。不执行Everything。
- 工具环境阻止启动Everything.exe，未更换方式绕过。真实Everything运行、漏扫、速度及无弹窗尚未验收。
- 工具环境也阻止正式8765API重启：后端源码已修改但旧服务尚未加载。新页面探测不到组件接口时禁用提交并提示重启服务，避免新旧接口混用。
- 需要用户正常重启本地服务，并在EXE选择一个小目录手动验证Everything；不要求关闭安全软件/设置。不能把文件校验或模拟测试称为本体运行通过。

## 官方依据

https://www.voidtools.com/downloads/
https://www.voidtools.com/Everything-1.4.1.1032.sha256
https://www.voidtools.com/License.txt
https://www.voidtools.com/support/everything/command_line_options/

官方说明-create-file-list不启动搜索窗口，但本机行为未验收。以后要利用MFT/USN常驻索引，还需确认卷范围和服务权限，不在本次完成声明中。

## 恢复

I:/PKAS-backups/everything-intake-20260909：intake.py/api.py/ClassifiedIntake.tsx/HANDOFF.md逐文件hash核对，web-dist副本，主库在线备份quick_check=ok。
新增everything_scanner.py、test_everything_scanner.py、tools/everything与本次报告；未接入个人目录、未更改原件、未恢复自动任务。
回退先停对应服务，保护后续合法修改后恢复受影响源码/资源；不直接用旧库覆盖新资料。
