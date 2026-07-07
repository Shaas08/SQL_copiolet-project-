"""
Query history.

Persists every request the copilot handles (prompt, generated SQL,
explanation) plus feedback on it (accepted / rejected / user-edited),
in a small local JSON file. Two things this enables:

1. `relevant_examples()` finds past requests similar to a new one and
   feeds them back into the LLM prompt as few-shot examples -- so if an
   engineer corrects "top customers" once, the corrected version (not
   the original wrong one) shows up as an example next time someone
   asks something similar.

2. A visible audit trail of what the copilot has generated and whether
   it was trusted, which is useful on its own for a team evaluating it.

Storage is a flat JSON file for simplicity/portability. For heavier use
(many users, need to query across a team) swap this for a real table in
your MySQL database -- the interface below (add_entry / mark_feedback /
recent / relevant_examples) is the seam to keep if you do that.
"""
import json
import os
import re
import time
import uuid

DEFAULT_HISTORY_PATH = os.path.join(os.path.dirname(__file__), ".history.json")

STOPWORDS = {
    "the", "a", "an", "of", "for", "by", "in", "on", "to", "and", "or",
    "with", "from", "show", "me", "get", "select", "all", "list"
}


def _tokenize(text):
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


class HistoryStore:
    def __init__(self, path=None):
        self.path = path or DEFAULT_HISTORY_PATH
        self._entries = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._entries, f, indent=2)

    def add_entry(self, user_input, sql, explanation):
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "user_input": user_input,
            "sql": sql,
            "explanation": explanation,
            "accepted": None,       # True / False / "edited" / None (no feedback yet)
            "edited_sql": None,
        }
        self._entries.append(entry)
        self._save()
        return entry["id"]

    def mark_feedback(self, entry_id, accepted, edited_sql=None):
        for e in self._entries:
            if e["id"] == entry_id:
                e["accepted"] = accepted
                if edited_sql:
                    e["edited_sql"] = edited_sql
                self._save()
                return True
        return False

    def recent(self, limit=10):
        return self._entries[-limit:]

    def relevant_examples(self, user_input, k=3):
        """
        Simple keyword-overlap ranking (Jaccard similarity on tokenized
        words) against past entries, preferring ones with positive
        feedback. No embeddings/vector search -- deliberately simple,
        swap in a real similarity search if your history grows large
        (hundreds+ of entries) or overlap-based matching starts missing
        obviously-related phrasings.
        """
        query_tokens = _tokenize(user_input)
        if not query_tokens:
            return []

        scored = []
        for e in self._entries:
            if e["accepted"] is False:
                continue  # never resurface something explicitly marked wrong
            entry_tokens = _tokenize(e["user_input"])
            if not entry_tokens:
                continue
            overlap = query_tokens & entry_tokens
            union = query_tokens | entry_tokens
            jaccard = len(overlap) / len(union) if union else 0
            if jaccard <= 0:
                continue
            # Prefer confirmed-good or corrected examples over never-reviewed ones.
            trust_bonus = 0.15 if e["accepted"] in (True, "edited") else 0
            scored.append((jaccard + trust_bonus, e))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for _, e in scored[:k]:
            sql = e["edited_sql"] if e["accepted"] == "edited" and e.get("edited_sql") else e["sql"]
            results.append({"user_input": e["user_input"], "sql": sql})
        return results
