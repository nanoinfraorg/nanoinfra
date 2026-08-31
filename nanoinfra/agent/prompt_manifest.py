"""What one turn's prompt is made of, section by section (#203).

The question this answers is *where did 31K tokens go*, and the reason it needs a module is that by
the time a request reaches a provider the answer is gone: the system prompt is one string in a flat
list of messages, and the tool schemas are a separate array with no notion of which server produced
which entry. Attribution has to be recorded **while the prompt is assembled**, or reconstructed by
somebody with an SSH session and a hand-written SQLite query -- which is exactly how the numbers in
`proposals/prompt-cost-and-visibility.md` were obtained, and exactly why this exists.

Deliberately structural: a section carries a **name and a size**, never its content. A manifest is
displayed in a browser and persisted with the turn, and a prompt holds `MEMORY.md`, `AGENTS.md` and
the user's own history -- so a record of the prompt that carried its text would be a second copy of
the conversation living somewhere nobody expects one. The same rule the call store follows, for the
same reason.

Tokens are estimated, not measured: the exact number is the provider's and it does not itemise. The
estimate is the same tokenizer the compaction decision already trusts, and the manifest says which
of the two it is showing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Sections below this are folded into the parent's total rather than listed. A panel with forty
#: two-token rows answers nothing that the group's own total does not.
MIN_LISTED_TOKENS = 8


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One named part of a prompt, and how big it is.

    `group` is what a reader collapses by -- `system`, `tools`, `messages`. `detail` is the sub-name
    inside the group, so the tool schemas of one MCP server land under `tools` with the server's own
    name rather than as forty siblings of `AGENTS.md`.
    """

    name: str
    chars: int
    tokens: int
    group: str = "system"
    detail: str = ""
    #: How many things this section stands for -- skills, tools, messages. Zero means "not a count",
    #: which is different from "none": a section is one thing unless it says otherwise.
    items: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "chars": self.chars,
            "tokens": self.tokens,
            "group": self.group,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.items:
            payload["items"] = self.items
        return payload


@dataclass
class PromptManifest:
    """Every section of one turn's prompt, in the order it was assembled.

    Order matters and is preserved: prefix caching reuses a *prefix*, so "what comes before the
    volatile part" is the question a reader of this is most likely to be asking.
    """

    sections: list[PromptSection] = field(default_factory=list[PromptSection])
    #: True when the token figures came from the provider rather than from our tokenizer. Always
    #: False today, and present so the panel can stop saying "estimated" if that ever changes.
    measured: bool = False

    def add(
        self,
        name: str,
        text: str,
        *,
        group: str = "system",
        detail: str = "",
        items: int = 0,
    ) -> None:
        """Record one section. An empty one is skipped rather than listed as zero."""
        if not text:
            return
        tokens = estimate_tokens(text)
        if tokens < MIN_LISTED_TOKENS and not detail:
            # Folded, not dropped: the total still counts it. A three-token section is noise in a
            # list whose point is finding the twenty-thousand-token one.
            self.sections.append(
                PromptSection(name=name, chars=len(text), tokens=tokens, group=group, items=items)
            )
            return
        self.sections.append(
            PromptSection(
                name=name,
                chars=len(text),
                tokens=tokens,
                group=group,
                detail=detail,
                items=items,
            )
        )

    def add_counted(
        self, name: str, *, chars: int, tokens: int, group: str, detail: str = "", items: int = 0
    ) -> None:
        """Record a section whose size is already known, for a caller that measured it itself."""
        if tokens <= 0 and chars <= 0:
            return
        self.sections.append(
            PromptSection(
                name=name, chars=chars, tokens=tokens, group=group, detail=detail, items=items
            )
        )

    def total_tokens(self) -> int:
        return sum(section.tokens for section in self.sections)

    def group_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for section in self.sections:
            totals[section.group] = totals.get(section.group, 0) + section.tokens
        return totals

    def as_dict(self) -> dict[str, Any]:
        """The shape the WebUI reads. Names and numbers, and nothing else."""
        return {
            "sections": [section.as_dict() for section in self.sections],
            "groups": self.group_totals(),
            "total_tokens": self.total_tokens(),
            "measured": self.measured,
        }

    def summary_line(self) -> str:
        """One line for a log, because the first reader of this is usually a terminal."""
        groups = self.group_totals()
        parts = [f"{name} {tokens:,}" for name, tokens in sorted(groups.items())]
        return f"prompt {self.total_tokens():,} tokens ({', '.join(parts)})"


def estimate_tokens(text: str) -> int:
    """Estimate one section's tokens with the tokenizer the rest of the tree already uses.

    Thin on purpose: the fallback and the encoding live in one place, beside the truncation helpers
    that already depend on them.
    """
    from nanoinfra.utils.helpers import count_text_tokens

    return count_text_tokens(text)


__all__ = [
    "MIN_LISTED_TOKENS",
    "PromptManifest",
    "PromptSection",
    "estimate_tokens",
]
