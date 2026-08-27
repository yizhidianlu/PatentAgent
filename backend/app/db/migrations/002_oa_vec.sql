-- 002_oa_vec.sql —— OA 案例库检索层（模块 D）
--
-- 设计（backend-architecture.md §2 + §6 风险 4）：
--   * `oa_vec`（sqlite-vec 的 vec0 虚拟表）**不在本文件建**：它的维度写死在 DDL 里
--     （`embedding float[{dim}]`），而维度来自设置页的 embedding 配置，只能在运行时
--     由 `services/vector.ensure_vec_table(dim)` 动态创建；换模型/换维度时 DROP 重建
--     （见 `services/vector.rebuild()` 与 `POST /settings/embedding/reindex`）。
--     运行时 DDL 形如：
--       CREATE VIRTUAL TABLE oa_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dim}]);
--   * `oa_vec_blob` 是**可移植的向量原始存储**（float32 小端字节串，与 vec0 的输入格式
--     一致）：sqlite-vec 扩展加载失败时（Windows/Py3.13 可能装不上）纯 Python 余弦
--     暴搜就读这张表，向量数据不因扩展缺失而丢失；扩展就绪后 rebuild 即可回填 oa_vec。
--   * `oa_vec_meta` 记录当前索引的维度与模型，供 /oa/library/search 诊断与重建判定。

CREATE TABLE IF NOT EXISTS oa_vec_blob (
  chunk_id   INTEGER PRIMARY KEY REFERENCES oa_chunks(id) ON DELETE CASCADE,
  library_id TEXT    NOT NULL,
  dim        INTEGER NOT NULL,
  embedding  BLOB    NOT NULL,          -- struct.pack('<{dim}f', *vector)，已 L2 归一化
  model      TEXT    NOT NULL DEFAULT '',
  created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oa_vec_blob_lib ON oa_vec_blob(library_id);

CREATE TABLE IF NOT EXISTS oa_vec_meta (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  dim        INTEGER NOT NULL,
  model      TEXT    NOT NULL DEFAULT '',
  updated_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oa_chunks_lib ON oa_chunks(library_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_oa_library_status ON oa_library(status, updated_at DESC);
