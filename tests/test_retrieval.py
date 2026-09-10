import unittest

from zhishu.retrieval import SearchHit, reciprocal_rank_fusion


class RetrievalFusionTests(unittest.TestCase):
    def test_fuses_duplicate_sources_and_preserves_channels(self) -> None:
        lexical = SearchHit("doc-1", "精确结果", "file:1", "fts")
        semantic = SearchHit("doc-1", "语义结果", "file:1", "vector")
        vector_only = SearchHit("doc-2", "相近结果", "file:2", "vector")

        results = reciprocal_rank_fusion(
            {"fts": [lexical], "vector": [semantic, vector_only]}, limit=10
        )

        self.assertEqual([item.source_id for item in results], ["doc-1", "doc-2"])
        self.assertEqual(results[0].channels, ("fts", "vector"))

    def test_channel_weights_change_order_without_changing_sources(self) -> None:
        catalog = SearchHit("file-1", "文件", "disk:file-1", "catalog")
        vector = SearchHit("doc-1", "正文", "document:1", "vector")

        results = reciprocal_rank_fusion(
            {"catalog": [catalog], "vector": [vector]},
            weights={"catalog": 0.5, "vector": 2.0},
        )

        self.assertEqual([item.source_id for item in results], ["doc-1", "file-1"])


if __name__ == "__main__":
    unittest.main()

