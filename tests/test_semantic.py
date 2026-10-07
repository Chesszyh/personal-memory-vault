import importlib.util
import tempfile
import unittest
from pathlib import Path

from personal_vault.recall import RecallRepository
from personal_vault.semantic import SemanticIndex, chunks, fuse_ranks
from tests.test_reader import ReaderFixture


class ChunkTests(unittest.TestCase):
    def test_rank_fusion_keeps_both_signals(self):
        ranked=fuse_ranks([('vector_best',1),('agreed',.9),('rerank_best',.8)],[.1,.5,1])
        self.assertEqual({item[0] for item in ranked},{'vector_best','agreed','rerank_best'})
        self.assertEqual(ranked[0][1],ranked[1][1])
    def test_overlapping_chunks_preserve_offsets_and_tail(self):
        text='a'*600+'b'*301
        parts=list(chunks(text))
        self.assertEqual([offset for offset,_ in parts],[0,500])
        self.assertEqual(parts[-1][1],text[500:])


@unittest.skipUnless(importlib.util.find_spec('numpy'),'optional semantic dependencies not installed')
class SemanticTests(unittest.TestCase):
    def test_incremental_build_exclusion_and_source_budget(self):
        import numpy as np
        class Encoder:
            calls=0
            def encode_document(self,texts,**kwargs):
                self.calls+=1
                return np.array([[1.,0.] for _ in texts],dtype=np.float32)
            def encode_query(self,texts,**kwargs):
                return np.array([[1.,0.]],dtype=np.float32)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);ReaderFixture(root)
            index=SemanticIndex(root);index._encoder=Encoder()
            first=index.build();calls=index._encoder.calls
            self.assertGreater(first['indexed_messages'],0)
            self.assertEqual(index.build()['indexed_messages'],0)
            self.assertEqual(index._encoder.calls,calls)
            result=index.search('paraphrase',rerank=False,budget_chars=500)
            self.assertLessEqual(result['returned_text_characters'],500)
            ids=[r['conversation_id'] for r in result['items']]
            self.assertEqual(len(ids),len(set(ids)))
            hit=result['items'][0]
            self.assertIn('?source=',hit['reader_url'])
            RecallRepository(root).feedback(hit['message_id'],'excluded')
            self.assertNotIn(hit['message_id'],[r['message_id'] for r in index.search('paraphrase',rerank=False)['items']])
