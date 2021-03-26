# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""
This file contains the Python portion of the template rendering code.

Templates can have filters applied to field replacements. The Rust template
rendering code will apply any built in filters, and stop at the first
unrecognized filter. The remaining filters are returned to Python,
and applied using the hook system. For example,
{{myfilter:hint:text:Field}} will apply the built in text and hint filters,
and then attempt to apply myfilter. If no add-ons have provided the filter,
the filter is skipped.

Add-ons can register a filter with the following code:

from anki import hooks
hooks.field_filter.append(myfunc)

This will call myfunc, passing the field text in as the first argument.
Your function should decide if it wants to modify the text by checking
the filter_name argument, and then return the text whether it has been
modified or not.

A Python implementation of the standard filters is currently available in the
template_legacy.py file, using the legacy addHook() system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import anki
import anki._backend.backend_pb2 as _pb
from anki import hooks
from anki.cards import Card
from anki.decks import DeckManager
from anki.errors import TemplateError
from anki.models import NoteType
from anki.notes import Note
from anki.sound import AVTag, SoundOrVideoTag, TTSTag
from anki.utils import to_json_bytes

CARD_BLANK_HELP = (
    "https://anki.tenderapp.com/kb/card-appearance/the-front-of-this-card-is-blank"
)


@dataclass
class TemplateReplacement:
    field_name: str
    current_text: str
    filters: List[str]


TemplateReplacementList = List[Union[str, TemplateReplacement]]


@dataclass
class PartiallyRenderedCard:
    qnodes: TemplateReplacementList
    anodes: TemplateReplacementList

    @classmethod
    def from_proto(cls, out: _pb.RenderCardOut) -> PartiallyRenderedCard:
        qnodes = cls.nodes_from_proto(out.question_nodes)
        anodes = cls.nodes_from_proto(out.answer_nodes)

        return PartiallyRenderedCard(qnodes, anodes)

    @staticmethod
    def nodes_from_proto(
        nodes: Sequence[_pb.RenderedTemplateNode],
    ) -> TemplateReplacementList:
        results: TemplateReplacementList = []
        for node in nodes:
            if node.WhichOneof("value") == "text":
                results.append(node.text)
            else:
                results.append(
                    TemplateReplacement(
                        field_name=node.replacement.field_name,
                        current_text=node.replacement.current_text,
                        filters=list(node.replacement.filters),
                    )
                )
        return results


def av_tag_to_native(tag: _pb.AVTag) -> AVTag:
    val = tag.WhichOneof("value")
    if val == "sound_or_video":
        return SoundOrVideoTag(filename=tag.sound_or_video)
    else:
        return TTSTag(
            field_text=tag.tts.field_text,
            lang=tag.tts.lang,
            voices=list(tag.tts.voices),
            other_args=list(tag.tts.other_args),
            speed=tag.tts.speed,
        )


def av_tags_to_native(tags: Sequence[_pb.AVTag]) -> List[AVTag]:
    return list(map(av_tag_to_native, tags))


class TemplateRenderContext:
    """Holds information for the duration of one card render.

    This may fetch information lazily in the future, so please avoid
    using the _private fields directly."""

    @staticmethod
    def from_existing_card(card: Card, browser: bool) -> TemplateRenderContext:
        return TemplateRenderContext(card.col, card, card.note(), browser)

    @classmethod
    def from_card_layout(
        cls,
        note: Note,
        card: Card,
        notetype: NoteType,
        template: Dict,
        fill_empty: bool,
    ) -> TemplateRenderContext:
        return TemplateRenderContext(
            note.col,
            card,
            note,
            notetype=notetype,
            template=template,
            fill_empty=fill_empty,
        )

    def __init__(
        self,
        col: anki.collection.Collection,
        card: Card,
        note: Note,
        browser: bool = False,
        notetype: NoteType = None,
        template: Optional[Dict] = None,
        fill_empty: bool = False,
    ) -> None:
        self._col = col.weakref()
        self._card = card
        self._note = note
        self._browser = browser
        self._template = template
        self._fill_empty = fill_empty
        self._fields: Optional[Dict] = None
        if not notetype:
            self._note_type = note.model()
        else:
            self._note_type = notetype

        # if you need to store extra state to share amongst rendering
        # hooks, you can insert it into this dictionary
        self.extra_state: Dict[str, Any] = {}

    def col(self) -> anki.collection.Collection:
        return self._col

    def fields(self) -> Dict[str, str]:
        print(".fields() is obsolete, use .note() or .card()")
        if not self._fields:
            # fields from note
            fields = dict(self._note.items())

            # add (most) special fields
            fields["Tags"] = self._note.stringTags().strip()
            fields["Type"] = self._note_type["name"]
            fields["Deck"] = self._col.decks.name(self._card.current_deck_id())
            fields["Subdeck"] = DeckManager.basename(fields["Deck"])
            if self._template:
                fields["Card"] = self._template["name"]
            else:
                fields["Card"] = ""
            flag = self._card.user_flag()
            fields["CardFlag"] = flag and f"flag{flag}" or ""
            self._fields = fields

        return self._fields

    def card(self) -> Card:
        """Returns the card being rendered.

        Be careful not to call .q() or .a() on the card, or you'll create an
        infinite loop."""
        return self._card

    def note(self) -> Note:
        return self._note

    def note_type(self) -> NoteType:
        return self._note_type

    # legacy
    def qfmt(self) -> str:
        return templates_for_card(self.card(), self._browser)[0]

    # legacy
    def afmt(self) -> str:
        return templates_for_card(self.card(), self._browser)[1]

    def render(self) -> TemplateRenderOutput:
        try:
            partial = self._partially_render()
        except TemplateError as e:
            return TemplateRenderOutput(
                question_text=str(e),
                answer_text=str(e),
                question_av_tags=[],
                answer_av_tags=[],
            )

        qtext = apply_custom_filters(partial.qnodes, self, front_side=None)
        qout = self.col()._backend.extract_av_tags(text=qtext, question_side=True)

        atext = apply_custom_filters(partial.anodes, self, front_side=qout.text)
        aout = self.col()._backend.extract_av_tags(text=atext, question_side=False)

        output = TemplateRenderOutput(
            question_text=qout.text,
            answer_text=aout.text,
            question_av_tags=av_tags_to_native(qout.av_tags),
            answer_av_tags=av_tags_to_native(aout.av_tags),
            css=self.note_type()["css"],
        )

        if not self._browser:
            hooks.card_did_render(output, self)

        return output

    def _partially_render(self) -> PartiallyRenderedCard:
        if self._template:
            # card layout screen
            out = self._col._backend.render_uncommitted_card(
                note=self._note._to_backend_note(),
                card_ord=self._card.ord,
                template=to_json_bytes(self._template),
                fill_empty=self._fill_empty,
            )
        else:
            # existing card (eg study mode)
            out = self._col._backend.render_existing_card(
                card_id=self._card.id, browser=self._browser
            )
        return PartiallyRenderedCard.from_proto(out)


@dataclass
class TemplateRenderOutput:
    "Stores the rendered templates and extracted AV tags."
    question_text: str
    answer_text: str
    question_av_tags: List[AVTag]
    answer_av_tags: List[AVTag]
    css: str = ""

    def question_and_style(self) -> str:
        return f"<style>{self.css}</style>{self.question_text}"

    def answer_and_style(self) -> str:
        return f"<style>{self.css}</style>{self.answer_text}"


# legacy
def templates_for_card(card: Card, browser: bool) -> Tuple[str, str]:
    template = card.template()
    if browser:
        q, a = template.get("bqfmt"), template.get("bafmt")
    else:
        q, a = None, None
    q = q or template.get("qfmt")
    a = a or template.get("afmt")
    return q, a  # type: ignore


def apply_custom_filters(
    rendered: TemplateReplacementList,
    ctx: TemplateRenderContext,
    front_side: Optional[str],
) -> str:
    "Complete rendering by applying any pending custom filters."
    # template already fully rendered?
    if len(rendered) == 1 and isinstance(rendered[0], str):
        return rendered[0]

    res = ""
    for node in rendered:
        if isinstance(node, str):
            res += node
        else:
            # do we need to inject in FrontSide?
            if node.field_name == "FrontSide" and front_side is not None:
                node.current_text = front_side

            field_text = node.current_text
            for filter_name in node.filters:
                field_text = hooks.field_filter(
                    field_text, node.field_name, filter_name, ctx
                )
                # legacy hook - the second and fifth argument are no longer used.
                field_text = anki.hooks.runFilter(
                    f"fmod_{filter_name}",
                    field_text,
                    "",
                    ctx.note().items(),
                    node.field_name,
                    "",
                )

            res += field_text
    return res
