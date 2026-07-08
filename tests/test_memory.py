"""Tests for Phase 3: memory pipeline (short-term, long-term, hippocampus)."""
import sys
import os
import tempfile
from pathlib import Path

src = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src))

import pytest
from character_chat.memory.schema import MemoryEntry, MemoryType, MemoryImportance
from character_chat.memory.short_term import ShortTermMemory
from character_chat.memory.long_term import LongTermMemory
from character_chat.memory.hippocampus import HippocampusConsolidator
from character_chat.memory.pipeline import MemoryPipeline


class TestMemorySchema:
    """Test memory entry schema."""

    def test_create_entry(self):
        entry = MemoryEntry(
            content="User: hello\nCharacter: hi",
            importance=MemoryImportance.NORMAL,
            emotion_label="happy",
            scene="天台",
            time_phase="黄昏",
        )
        assert entry.content == "User: hello\nCharacter: hi"
        assert entry.importance == MemoryImportance.NORMAL
        assert entry.emotion_label == "happy"

    def test_to_dict_roundtrip(self):
        entry = MemoryEntry(
            content="test",
            importance=MemoryImportance.HIGH,
            emotion_label="sad",
            scene="家",
            time_phase="夜",
        )
        d = entry.to_dict()
        restored = MemoryEntry.from_dict(d)
        assert restored.content == entry.content
        assert restored.importance == entry.importance
        assert restored.emotion_label == entry.emotion_label


class TestShortTermMemory:
    """Test in-memory buffer."""

    def test_add_and_get(self):
        stm = ShortTermMemory(max_entries=5)
        stm.add("memory 1")
        stm.add("memory 2")
        recent = stm.get_recent(2)
        assert len(recent) == 2
        assert recent[0].content == "memory 1"
        assert recent[1].content == "memory 2"

    def test_max_entries_eviction(self):
        stm = ShortTermMemory(max_entries=3)
        for i in range(5):
            stm.add(f"memory {i}")
        assert stm.size == 3
        all_entries = stm.get_all()
        assert all_entries[0].content == "memory 2"  # oldest remaining
        assert all_entries[2].content == "memory 4"

    def test_consolidation_candidates(self):
        stm = ShortTermMemory(max_entries=10)
        # Add 8 entries
        for i in range(8):
            stm.add(f"memory {i}", importance=MemoryImportance.LOW)
        # Add 1 HIGH importance
        stm.add("important!", importance=MemoryImportance.HIGH)
        candidates = stm.get_consolidation_candidates()
        # HIGH importance should always be a candidate
        assert any(e.content == "important!" for e in candidates)
        # Older entries (outside active window) should be candidates
        assert len(candidates) >= 2

    def test_remove_entries(self):
        stm = ShortTermMemory(max_entries=10)
        e1 = stm.add("a")
        e2 = stm.add("b")
        stm.remove([e1])
        assert stm.size == 1
        assert stm.get_all()[0].content == "b"

    def test_emotion_and_scene_metadata(self):
        stm = ShortTermMemory()
        entry = stm.add("test", emotion_label="sad", scene="天台", time_phase="黄昏")
        assert entry.emotion_label == "sad"
        assert entry.scene == "天台"
        assert entry.time_phase == "黄昏"


class TestLongTermMemory:
    """Test sqlite + TF-IDF storage."""

    def _make_ltm(self):
        tmp = tempfile.mktemp(suffix=".db")
        return LongTermMemory(db_path=tmp)

    def test_store_and_count(self):
        ltm = self._make_ltm()
        entry = MemoryEntry(content="今天在天台看了夕阳", importance=MemoryImportance.NORMAL)
        row_id = ltm.store(entry)
        assert row_id == 1
        assert ltm.count() == 1
        ltm.close()

    def test_search_finds_relevant(self):
        ltm = self._make_ltm()
        # Store some memories (long enough that TF-IDF character n-grams work)
        ltm.store(MemoryEntry(content="今天放学后我去了天台看夕阳,她轻声哼了一下"))
        ltm.store(MemoryEntry(content="数学考试我考了95分,她恭喜了我"))
        ltm.store(MemoryEntry(content="放学后我去图书馆做了一会儿作业"))

        # Search for "夕阳" (character-level TF-IDF finds partial overlap)
        results = ltm.search("夕阳天台", top_k=3)
        assert len(results) >= 1
        top_entry, score = results[0]
        assert score > 0
        assert any("天台" in e.content for e, _ in results)

        ltm.close()

    def test_search_returns_empty_on_empty_db(self):
        ltm = self._make_ltm()
        results = ltm.search("anything")
        assert results == []
        ltm.close()

    def test_store_batch(self):
        ltm = self._make_ltm()
        entries = [
            MemoryEntry(content="记忆1"),
            MemoryEntry(content="记忆2"),
            MemoryEntry(content="记忆3"),
        ]
        ids = ltm.store_batch(entries)
        assert len(ids) == 3
        assert ltm.count() == 3
        ltm.close()


class TestHippocampusConsolidator:
    """Test consolidation from short-term to long-term."""

    def _make_pipeline_parts(self):
        tmp = tempfile.mktemp(suffix=".db")
        stm = ShortTermMemory(max_entries=10)
        ltm = LongTermMemory(db_path=tmp)
        hippo = HippocampusConsolidator(short_term=stm, long_term=ltm)
        return stm, ltm, hippo

    def test_consolidate_moves_entries(self):
        stm, ltm, hippo = self._make_pipeline_parts()
        stm.add("old memory 1", importance=MemoryImportance.LOW)
        stm.add("old memory 2", importance=MemoryImportance.LOW)
        stm.add("recent memory", importance=MemoryImportance.LOW)

        # Use force_all to consolidate all 3 (otherwise active window retains 5)
        n = hippo.consolidate(force_all=True)
        assert n == 3
        assert ltm.count() == 3
        ltm.close()

    def test_consolidate_force_all(self):
        stm, ltm, hippo = self._make_pipeline_parts()
        stm.add("memory A", importance=MemoryImportance.HIGH, emotion_label="happy")
        stm.add("memory B", importance=MemoryImportance.NORMAL)

        n = hippo.consolidate(force_all=True)
        assert n == 2
        assert ltm.count() == 2
        assert stm.size == 0  # all removed from short-term
        ltm.close()

    def test_consolidate_boosts_emotional_importance(self):
        stm, ltm, hippo = self._make_pipeline_parts()
        stm.add("sad memory", importance=MemoryImportance.LOW, emotion_label="sad")

        hippo.consolidate(force_all=True)
        # The entry should have been boosted to NORMAL
        all_ltm = ltm.get_all()
        assert any(e.importance == MemoryImportance.NORMAL for e in all_ltm)
        ltm.close()

    def test_consolidate_deduplicates(self):
        stm, ltm, hippo = self._make_pipeline_parts()
        # Store same content in long-term first
        ltm.store(MemoryEntry(content="duplicate memory content here"))

        # Try to consolidate the same content
        stm.add("duplicate memory content here")
        n = hippo.consolidate(force_all=True)
        # Should be skipped as duplicate
        assert ltm.count() == 1
        ltm.close()


class TestMemoryPipeline:
    """Test end-to-end memory pipeline."""

    def _make_pipeline(self):
        tmp = tempfile.mktemp(suffix=".db")
        return MemoryPipeline(db_path=tmp, max_short_term=10)

    def test_record_and_recall(self):
        pipe = self._make_pipeline()
        pipe.record(
            user_text="我今天数学考了满分",
            reply_text="哼，还不错嘛",
            emotion_label="happy",
            scene="教室",
            time_phase="午",
        )

        # Consolidate first so memory goes to long-term store
        pipe.consolidate(force_all=True)

        # Recall should find it (use character-level overlap for Chinese)
        memories = pipe.recall("我今天数学考了满分", top_k=3)
        assert len(memories) >= 1
        assert any("数学" in m for m in memories)
        pipe.close()

    def test_consolidate(self):
        pipe = self._make_pipeline()
        pipe.record("你好", "哼", emotion_label="neutral")
        pipe.record("我奶奶生病了", "……你没事吧", emotion_label="sad")

        n = pipe.consolidate(force_all=True)
        assert n >= 1
        stats = pipe.get_stats()
        assert stats["long_term_size"] >= 1
        pipe.close()

    def test_emotional_auto_boost(self):
        pipe = self._make_pipeline()
        # Recording with non-neutral emotion should auto-boost to HIGH
        pipe.record(
            user_text="我奶奶住院了",
            reply_text="……",
            emotion_label="sad",
        )
        entries = pipe.short_term.get_all()
        assert any(e.importance == MemoryImportance.HIGH for e in entries)
        pipe.close()

    def test_recall_with_no_memories(self):
        pipe = self._make_pipeline()
        memories = pipe.recall("anything")
        assert memories == []
        pipe.close()
