# 统一检索交付（2026-09-08）

EXE资料底座、API普通搜索/搜索实验台与MCP search_knowledge使用同一个
search_gateway.unified_search → RetrievalService。默认文件范围、受限范围、
重排策略、上下文扩展一致；任务请求从全文及向量候选中排除。
旧MCP data列表保留，新增retrieval字段包含与EXE相同的完整pkas.search.v1响应。

## 当前验证

- 本机普通关键词PKAS：HTTP和MCP注册工具分发均mode=hybrid，前5条编号/排序、
  warnings、parameters相同。该次前5条通道均vector，不能说每条同时全文命中。
- 真实页面显示hybrid并返回10条；截图reports/unified-search-live-20260908.png。
- 全文降级与正常语义结果有隔离对比测试；既有重排回归覆盖其逻辑。
- MCP工具分发已实测，但没有重新启用用户Codex中的MCP配置，也未验证真实任务主动检索。
- 真实验证关闭重排以控制成本；调用了少量已配置Embedding API，没有全库重嵌入。

## 明确边界

- 文件支持混合；聊天仍只有FTS，显式警告，不能宣称聊天也有向量索引。
- 向量不可用或没有向量命中会显示fts_only及提示；健康标签不是答案准确率。
- 本地旧LocalSearchService保留作离线导入验证帮助类，不再作为EXE正式搜索入口。
- 模型/索引变化可能使两次不同时间调用有差异；同源不等于整个Codex回答必然相同。
- 保留API与手动启动的Qdrant以便用户试用；未启动Core、同步、WeFlow或计划任务。

## 恢复

I:/PKAS-backups/unified-retrieval-20260908：受影响源码与构建副本；主库一致性备份
quick_check=ok；停机Qdrant存储567文件逐文件hash核对。新文件及测试副本见RESTORE.md。
仅重启既有API与启动Qdrant；未改变源文件、同步根、自动运行策略。
