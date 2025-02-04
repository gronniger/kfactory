"""CrossSections for KFactory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    NotRequired,
    Self,
    TypedDict,
    cast,
)

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from . import kdb
from .enclosure import DLayerEnclosure, LayerEnclosure, LayerEnclosureSpec
from .typings import TUnit

if TYPE_CHECKING:
    from .layout import KCLayout

__all__ = ["CrossSection", "DCrossSection", "SymmetricalCrossSection"]


class SymmetricalCrossSection(BaseModel, frozen=True):
    """CrossSection which is symmetrical to its main_layer/width."""

    width: int
    enclosure: LayerEnclosure
    name: str = ""
    radius: int | None = None
    radius_min: int | None = None
    bbox_sections: dict[kdb.LayerInfo, tuple[int, int, int, int]]

    def __init__(
        self,
        width: int,
        enclosure: LayerEnclosure,
        name: str | None = None,
        bbox_sections: dict[kdb.LayerInfo, tuple[int, int, int, int]] | None = None,
    ) -> None:
        """Initialized the CrossSection."""
        super().__init__(
            width=width,
            enclosure=enclosure,
            name=name or f"{enclosure.name}_{width}",
            bbox_sections=bbox_sections or {},
        )

    @model_validator(mode="before")
    @classmethod
    def _set_name(cls, data: Any) -> Any:
        data["name"] = data.get("name") or f"{data['enclosure'].name}_{data['width']}"
        return data

    @model_validator(mode="after")
    def _validate_enclosure_main_layer(self) -> Self:
        if self.enclosure.main_layer is None:
            raise ValueError("Enclosures of cross sections must have a main layer.")
        if (self.width // 2) * 2 != self.width:
            raise ValueError(
                "Width of symmetrical cross sections must have be a multiple of 2. "
                "This could cause cross sections and extrusions to become unsymmetrical"
                " otherwise."
            )
        return self

    @model_validator(mode="after")
    def _validate_width(self) -> Self:
        if self.width <= 0:
            raise ValueError("Width must be greater than 0.")
        return self

    @property
    def main_layer(self) -> kdb.LayerInfo:
        """Main Layer of the enclosure and cross section."""
        return self.enclosure.main_layer  # type: ignore[return-value]

    def to_dtype(self, kcl: KCLayout) -> DSymmetricalCrossSection:
        """Convert to a um based CrossSection."""
        return DSymmetricalCrossSection(
            width=kcl.to_um(self.width),
            enclosure=self.enclosure.to_dtype(kcl),
            name=self.name,
        )

    def get_xmax(self) -> int:
        return self.width // 2 + max(
            s.d_max
            for sections in self.enclosure.layer_sections.values()
            for s in sections.sections
        )


class TCrossSection(BaseModel, ABC, Generic[TUnit], frozen=True):
    _base: SymmetricalCrossSection = PrivateAttr()
    kcl: KCLayout
    name: str

    def __init__(
        self,
        width: TUnit,
        layer: kdb.LayerInfo,
        sections: list[tuple[TUnit, TUnit] | tuple[TUnit]],
        kcl: KCLayout,
        radius: TUnit | None = None,
        radius_min: TUnit | None = None,
        bbox_sections: Sequence[tuple[kdb.LayerInfo, TUnit, TUnit, TUnit, TUnit]] = [],
        base: SymmetricalCrossSection | None = None,
    ) -> None: ...

    @property
    @abstractmethod
    def width(self) -> TUnit: ...

    @property
    def layer(self) -> kdb.LayerInfo:
        return self._base.main_layer

    @property
    def enclosure(self) -> LayerEnclosure:
        return self._base.enclosure

    @property
    @abstractmethod
    def sections(self) -> dict[kdb.LayerInfo, list[tuple[TUnit | None, TUnit]]]: ...

    @property
    @abstractmethod
    def radius(self) -> TUnit | None: ...

    @property
    @abstractmethod
    def radius_min(self) -> TUnit | None: ...

    @property
    @abstractmethod
    def bbox_sections(
        self,
    ) -> dict[kdb.LayerInfo, tuple[TUnit, TUnit, TUnit, TUnit]]: ...

    @abstractmethod
    def get_xmin_xmax(self) -> tuple[TUnit, TUnit]: ...


class CrossSection(TCrossSection[int]):
    def __init__(
        self,
        width: int,
        layer: kdb.LayerInfo,
        sections: list[tuple[kdb.LayerInfo, int, int] | tuple[kdb.LayerInfo, int]],
        kcl: KCLayout,
        radius: int | None = None,
        radius_min: int | None = None,
        name: str | None = None,
        base: SymmetricalCrossSection | None = None,
        bbox_sections: list[tuple[kdb.LayerInfo, int, int, int, int]] | None = None,
    ) -> None:
        if base:
            base = kcl.get_cross_section(base)
        else:
            base = kcl.get_cross_section(
                SymmetricalCrossSection(
                    width=width,
                    enclosure=LayerEnclosure(sections=sections, main_layer=layer),
                    name=name,
                )
            )
        bbox_sections = bbox_sections or []
        BaseModel.__init__(
            self,
            name=name or base.name,
            kcl=kcl,
            bbox_sections={s[0]: (s[1], s[2], s[3], s[4]) for s in bbox_sections},
        )
        self._base = base

    @property
    def sections(self) -> dict[kdb.LayerInfo, list[tuple[int | None, int]]]:
        items = self._base.enclosure.layer_sections.items()
        return {
            layer: [(section.d_min, section.d_max) for section in sections.sections]
            for layer, sections in items
        }

    @property
    def bbox_sections(self) -> dict[kdb.LayerInfo, tuple[int, int, int, int]]:
        return self._base.bbox_sections.copy()

    @property
    def width(self) -> int:
        return self._base.width

    @property
    def radius(self) -> int | None:
        return self._base.radius

    @property
    def radius_min(self) -> int | None:
        return self._base.radius_min

    def get_xmin_xmax(self) -> tuple[int, int]:
        xmax = self._base.get_xmax()
        return (xmax, xmax)


class DCrossSection(TCrossSection[float]):
    def __init__(
        self,
        width: int,
        layer: kdb.LayerInfo,
        sections: list[tuple[kdb.LayerInfo, int, int] | tuple[kdb.LayerInfo, int]],
        kcl: KCLayout,
        radius: int | None = None,
        radius_min: int | None = None,
        name: str | None = None,
        base: SymmetricalCrossSection | None = None,
        bbox_sections: list[tuple[kdb.LayerInfo, int, int, int, int]] | None = None,
    ) -> None:
        bbox_sections = bbox_sections or []
        if base:
            base = kcl.get_cross_section(base)
        else:
            base = kcl.get_cross_section(
                SymmetricalCrossSection(
                    width=width,
                    enclosure=LayerEnclosure(sections=sections, main_layer=layer),
                    name=name,
                )
            )
        BaseModel.__init__(
            self,
            name=name or base.name,
            kcl=kcl,
            bbox_sections={s[0]: (s[1], s[2], s[3], s[4]) for s in bbox_sections},
        )
        self._base = base

    @property
    def sections(self) -> dict[kdb.LayerInfo, list[tuple[float | None, float]]]:
        items = self._base.enclosure.layer_sections.items()
        return {
            layer: [
                (
                    self.kcl.to_um(section.d_min)
                    if section.d_min is not None
                    else None,
                    self.kcl.to_um(section.d_max),
                )
                for section in sections.sections
            ]
            for layer, sections in items
        }

    @property
    def bbox_sections(self) -> dict[kdb.LayerInfo, tuple[float, float, float, float]]:
        return {
            k: tuple(self.kcl.to_um(e) for e in v)  # type: ignore[misc]
            for k, v in self._base.bbox_sections.items()
        }

    def get_xmin_xmax(self) -> tuple[float, float]:
        xmax = self.kcl.to_um(self._base.get_xmax())
        return (xmax, xmax)


class DSymmetricalCrossSection(BaseModel):
    """um based CrossSection."""

    width: float
    enclosure: DLayerEnclosure
    name: str | None = None

    @model_validator(mode="after")
    def _validate_width(self) -> Self:
        if self.width <= 0:
            raise ValueError("Width must be greater than 0.")
        return self

    def to_itype(self, kcl: KCLayout) -> SymmetricalCrossSection:
        """Convert to a dbu based CrossSection."""
        return SymmetricalCrossSection(
            width=kcl.to_dbu(self.width),
            enclosure=kcl.get_enclosure(self.enclosure.to_itype(kcl)),
            name=self.name,
        )


class CrossSectionSpec(TypedDict):
    name: NotRequired[str]
    sections: NotRequired[
        list[tuple[kdb.LayerInfo, int] | tuple[kdb.LayerInfo, int, int]]
    ]
    main_layer: kdb.LayerInfo
    width: int | float
    dsections: NotRequired[
        list[tuple[kdb.LayerInfo, float] | tuple[kdb.LayerInfo, float, float]]
    ]


class CrossSectionModel(BaseModel):
    cross_sections: dict[str, SymmetricalCrossSection] = Field(default_factory=dict)
    kcl: KCLayout

    def get_cross_section(
        self,
        cross_section: str
        | SymmetricalCrossSection
        | CrossSectionSpec
        | DSymmetricalCrossSection,
    ) -> SymmetricalCrossSection:
        if isinstance(
            cross_section, SymmetricalCrossSection
        ) and cross_section.enclosure != self.kcl.get_enclosure(
            cross_section.enclosure
        ):
            return self.get_cross_section(
                CrossSectionSpec(
                    sections=cross_section.enclosure.model_dump()["sections"],
                    main_layer=cross_section.main_layer,
                    name=cross_section.name,
                    width=cross_section.width,
                )
            )

        if isinstance(cross_section, str):
            return self.cross_sections[cross_section]
        elif isinstance(cross_section, DSymmetricalCrossSection):
            cross_section = cross_section.to_itype(self.kcl)
        elif isinstance(cross_section, dict):
            cast(CrossSectionSpec, cross_section)
            if "dsections" in cross_section:
                cross_section = SymmetricalCrossSection(
                    width=self.kcl.to_dbu(cross_section["width"]),
                    enclosure=self.kcl.layer_enclosures.get_enclosure(
                        enclosure=LayerEnclosureSpec(
                            dsections=cross_section["dsections"],
                            main_layer=cross_section["main_layer"],
                        ),
                        kcl=self.kcl,
                    ),
                    name=cross_section.get("name", None),
                )
            else:
                w = cross_section["width"]
                if not isinstance(w, int) and not w.is_integer():
                    raise ValueError(
                        "A CrossSectionSpec with 'sections' must have a width in dbu."
                    )
                cross_section = SymmetricalCrossSection(
                    width=int(w),
                    enclosure=self.kcl.layer_enclosures.get_enclosure(
                        LayerEnclosureSpec(
                            sections=cross_section.get("sections", []),
                            main_layer=cross_section["main_layer"],
                        ),
                        kcl=self.kcl,
                    ),
                    name=cross_section.get("name", None),
                )
        if cross_section.name not in self.cross_sections:
            self.cross_sections[cross_section.name] = cross_section
            return cross_section
        return self.cross_sections[cross_section.name]

    def __repr__(self) -> str:
        return repr(self.cross_sections)
