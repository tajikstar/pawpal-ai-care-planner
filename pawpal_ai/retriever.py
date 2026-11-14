"""
Knowledge base loading and retrieval.

Each file in knowledge/ has a small front-matter header (id, species, life_stage,
source) and "## " sections. Every section becomes one chunk. Retrieval scores
chunks with TF-IDF cosine similarity, then filters and boosts them by the pet's
species and life stage, so a senior cat never gets puppy advice.
"""

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_KNOWLEDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "my", "of", "on", "or", "should",
    "that", "the", "their", "this", "to", "what", "when", "with", "your", "you",
}

ROUTINE_LINE = re.compile(
    r"^- task:\s*(?P<task>.+?)\s*\|\s*time:\s*(?P<time>\d{2}:\d{2})\s*\|\s*"
    r"frequency:\s*(?P<frequency>\w+)\s*\|\s*why:\s*(?P<why>.+)$"
)


def tokenize(text: str) -> List[str]:
    """Lowercase words and numbers, minus stopwords."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


@dataclass
class Chunk:
    doc_id: str
    section: str
    text: str
    species: str
    life_stage: str
    source: str

    @property
    def chunk_id(self) -> str:
        """Stable citation id, e.g. 'dog-puppy#feeding'."""
        return f"{self.doc_id}#{self.section.lower().replace(' ', '-')}"

    def title_hint(self) -> str:
        return f"{self.species}, {self.life_stage}, {self.section}"


@dataclass
class RoutineItem:
    """A suggested task from a guide's Routine section (used by the offline planner)."""
    doc_id: str
    description: str
    time: str
    frequency: str
    why: str


@dataclass
class Retrieved:
    chunk: Chunk
    score: float


@dataclass
class KnowledgeBase:
    chunks: List[Chunk] = field(default_factory=list)
    routines: Dict[str, List[RoutineItem]] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Optional[str] = None) -> "KnowledgeBase":
        """Loads knowledge/*.md (default: the knowledge folder next to this package)."""
        folder = folder or DEFAULT_KNOWLEDGE_DIR
        kb = cls()
        for name in sorted(os.listdir(folder)):
            if name.endswith(".md"):
                with open(os.path.join(folder, name), encoding="utf8") as f:
                    kb._add_document(f.read().replace("\r\n", "\n"))
        kb._build_idf()
        return kb

    def _add_document(self, text: str) -> None:
        meta, body = _split_front_matter(text)
        doc_id = meta["id"]
        self.sources[doc_id] = meta.get("source", "")
        for section, section_text in _split_sections(body):
            if section.lower() == "routine":
                self.routines[doc_id] = _parse_routine(doc_id, section_text)
                continue
            self.chunks.append(Chunk(
                doc_id=doc_id,
                section=section,
                text=section_text,
                species=meta.get("species", "any"),
                life_stage=meta.get("life_stage", "any"),
                source=meta.get("source", ""),
            ))

    def _build_idf(self) -> None:
        self._vectors = []
        doc_freq = Counter()
        tokenized = [tokenize(c.section + " " + c.text) for c in self.chunks]
        for tokens in tokenized:
            doc_freq.update(set(tokens))
        n = len(self.chunks)
        self._idf = {w: math.log((1 + n) / (1 + df)) + 1 for w, df in doc_freq.items()}
        self._vectors = [self._tfidf(tokens) for tokens in tokenized]

    def _tfidf(self, tokens: List[str]) -> Dict[str, float]:
        counts = Counter(tokens)
        vec = {w: c * self._idf.get(w, 0.0) for w, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {w: v / norm for w, v in vec.items()}

    def species_supported(self, species: str) -> bool:
        return any(c.species == species for c in self.chunks)

    def retrieve(
        self,
        query: str,
        species: Optional[str] = None,
        life_stage: Optional[str] = None,
        top_k: int = 6,
        min_score: float = 0.05,
    ) -> List[Retrieved]:
        """
        Returns the top_k chunks for the query, best first.

        Chunks for a different species are dropped (species "any" always
        qualifies). A matching life stage adds a boost, a different life stage
        is penalized, so the right guide wins without hiding general advice.
        """
        q = self._tfidf(tokenize(query))
        results = []
        for chunk, vec in zip(self.chunks, self._vectors):
            if species and chunk.species not in (species, "any"):
                continue
            score = sum(weight * vec.get(word, 0.0) for word, weight in q.items())
            if life_stage and chunk.life_stage not in ("any",):
                score += 0.25 if chunk.life_stage == life_stage else -0.15
            if score >= min_score:
                results.append(Retrieved(chunk, round(score, 3)))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


def _split_front_matter(text: str):
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError("Knowledge file is missing its --- front matter --- header.")
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, match.group(2)


def _split_sections(body: str):
    sections = []
    for part in re.split(r"^## ", body, flags=re.MULTILINE)[1:]:
        heading, _, text = part.partition("\n")
        sections.append((heading.strip(), text.strip()))
    return sections


def _parse_routine(doc_id: str, text: str) -> List[RoutineItem]:
    items = []
    for line in text.splitlines():
        m = ROUTINE_LINE.match(line.strip())
        if m:
            items.append(RoutineItem(doc_id, m["task"], m["time"], m["frequency"], m["why"]))
    return items
