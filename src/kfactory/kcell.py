import abc
import functools
import importlib
import warnings
from abc import ABCMeta, abstractmethod
from dataclasses import InitVar, dataclass

# from enum import IntEnum
from enum import Enum
from hashlib import sha3_512
from inspect import signature
from pathlib import Path
from typing import (  # ParamSpec, # >= python 3.10
    Any,
    Callable,
    Concatenate,
    Generic,
    Hashable,
    Iterable,
    Iterator,
    Optional,
    Protocol,
    Type,
    TypeAlias,
    TypeGuard,
    TypeVar,
    Union,
    cast,
    overload,
)

import numpy as np
import ruamel.yaml
from cachetools import Cache, cached
from typing_extensions import ParamSpec

from . import kdb
from .port import rename_clockwise

KCellParams = ParamSpec("KCellParams")
OP = ParamSpec("OP")


def is_simple_port(port: "Port | DPort | ICplxPort | DCplxPort") -> "TypeGuard[Port]":
    return port.int_based() and not port.complex()


class PortWidthMismatch(ValueError):
    def __init__(
        self,
        inst: "Instance",
        other_inst: "Instance | Port | DPort | ICplxPort | DCplxPort",
        p1: "Port | DPort | ICplxPort | DCplxPort",
        p2: "Port | DPort | ICplxPort | DCplxPort",
        *args: Any,
    ):

        if isinstance(other_inst, Instance):
            super().__init__(
                f'Width mismatch between the ports {inst.cell.name}["{p1.name}"] and {other_inst.cell.name}["{p2.name}"] ({p1.width}/{p2.width})',
                *args,
            )
        else:
            super().__init__(
                f'Width mismatch between the ports {inst.cell.name}["{p1.name}"] and Port "{p2.name}" ({p1.width}/{p2.width})',
                *args,
            )


class PortLayerMismatch(ValueError):
    def __init__(
        self,
        lib: "KLib",
        inst: "Instance",
        other_inst: "Instance | Port | DPort | ICplxPort | DCplxPort",
        p1: "Port | DPort | ICplxPort | DCplxPort",
        p2: "Port | DPort | ICplxPort | DCplxPort",
        *args: Any,
    ):

        l1 = (
            f"{p1.layer.name}({p1.layer.__int__()})"
            if isinstance(p1.layer, LayerEnum)
            else str(lib.get_info(p1.layer))
        )
        l2 = (
            f"{p2.layer.name}({p2.layer.__int__()})"
            if isinstance(p2.layer, LayerEnum)
            else str(lib.get_info(p2.layer))
        )
        if isinstance(other_inst, Instance):
            super().__init__(
                f'Layer mismatch between the ports {inst.cell.name}["{p1.name}"] and {other_inst.cell.name}["{p2.name}"] ({l1}/{l2})',
                *args,
            )
        else:
            super().__init__(
                f'Layer mismatch between the ports {inst.cell.name}["{p1.name}"] and Port "{p2.name}" ({l1}/{l2})',
                *args,
            )


class PortTypeMismatch(ValueError):
    def __init__(
        self,
        inst: "Instance",
        other_inst: "Instance | Port | DPort | ICplxPort | DCplxPort",
        p1: "Port | DPort | ICplxPort | DCplxPort",
        p2: "Port | DPort | ICplxPort | DCplxPort",
        *args: Any,
    ):
        if isinstance(other_inst, Instance):
            super().__init__(
                f'Type mismatch between the ports {inst.cell.name}["{p1.name}"] and {other_inst.cell.name}["{p2.name}"] ({p1.port_type}/{p2.port_type})',
                *args,
            )
        else:
            super().__init__(
                f'Type mismatch between the ports {inst.cell.name}["{p1.name}"] and Port "{p2.name}" ({p1.port_type}/{p2.port_type})',
                *args,
            )


class FrozenError(AttributeError):
    """Raised if a KCell has been frozen and shouldn't be modified anymore"""

    pass


def default_save() -> kdb.SaveLayoutOptions:
    save = kdb.SaveLayoutOptions()
    save.gds2_write_cell_properties = True
    save.gds2_write_file_properties = True
    save.gds2_write_timestamps = False

    return save


class KLib(kdb.Layout):
    """This is a small extension to the ``klayout.db.Layout``. It adds tracking for the :py:class:`~kfactory.kcell.KCell` objects
    instead of only the :py:class:`klayout.db.Cell` objects. Additionally it allows creation and registration through :py:func:`~create_cell`

    All attributes of ``klayout.db.Layout`` are transparently accessible

    Attributes:
        editable: Whether the layout should be opened in editable mode (default: True)
        rename_function: function that takes an Iterable[Port] and renames them
    """

    def __init__(self, editable: bool = True) -> None:
        self.kcells: dict[str, "KCell"] = {}
        kdb.Layout.__init__(self, editable)
        self.rename_function: Callable[..., None] = rename_clockwise

    def create_cell(  # type: ignore[override]
        self,
        kcell: "KCell",
        name: str,
        *args: Union[
            list[str], list[Union[str, dict[str, Any]]], list[Union[str, str]]
        ],
        allow_duplicate: bool = False,
    ) -> kdb.Cell:
        """Create a new cell in the library. This shouldn't be called manually. The constructor of KCell will call this method.

        Args:
            kcell: The KCell to be registered in the Layout.
            name: The (initial) name of the cell. Can be changed through :py:func:`~update_cell_name`
            allow_duplicate: Allow the creation of a cell with the same name which already is registered in the Layout.\
            This will create a cell with the name :py:attr:`name` + `$1` or `2..n` increasing by the number of existing duplicates
            args: additional arguments passed to :py:func:`~klayout.db.Layout.create_cell`
            kwargs: additional keyword arguments passed to :py:func:`klayout.db.Layout.create_cell`

        Returns:
            klayout.db.Cell: klayout.db.Cell object created in the Layout

        """

        if allow_duplicate or (self.cell(name) is None):
            self.kcells[name] = kcell
            return kdb.Layout.create_cell(self, name, *args)
        else:
            raise ValueError(
                f"Cellname {name} already exists. Please make sure the cellname is unique or pass `allow_duplicate` when creating the library"
            )

    def register_cell(self, kcell: "KCell") -> None:
        """Register an existing cell in the KLib object

        Args:
            kcell: KCell to be registered in the KLib
        """

        if kcell.name in self.kcells and kcell is not self.kcells[kcell.name]:
            raise KeyError(
                "Cannot register a new cell with a name that already exists in the library"
            )

        else:
            self.kcells[kcell.name] = kcell

    def update_cell_name(self, name: str, new_name: str) -> None:
        if new_name != name:
            self.kcells[new_name] = self.kcells[name]
            del self.kcells[name]

    def read(
        self,
        filename: str,
        options: Optional[kdb.LoadLayoutOptions] = None,
        register_cells: bool = True,
    ) -> kdb.LayerMap:
        if register_cells:
            cells = set(self.cells("*"))
        fn = str(Path(filename).resolve())
        if options is None:
            lm = kdb.Layout.read(self, fn)
        else:
            lm = kdb.Layout.read(self, fn, options)

        if register_cells:
            new_cells = set(self.cells("*")) - cells
            for c in new_cells:
                self.register_cell(KCell(kdb_cell=c, library=self))

        return lm

    def write(  # type: ignore[override]
        self,
        filename: str | Path,
        gzip: bool = False,
        options: kdb.SaveLayoutOptions = default_save(),
    ) -> None:
        return kdb.Layout.write(self, str(filename), options)


library = (
    KLib()
)  #: Default library object. :py:class:`~kfactory.kcell.KCell` uses this object unless another one is specified in the constructor


class LayerEnum(int, Enum):

    layer: int
    datatype: int

    def __new__(  # type: ignore[misc]
        cls: "LayerEnum",
        layer: int,
        datatype: int,
        lib: KLib = library,
    ) -> "LayerEnum":
        value = lib.layer(layer, datatype)
        obj: int = int.__new__(cls, value)  # type: ignore[call-overload]
        obj._value_ = value  # type: ignore[attr-defined]
        obj.layer = layer  # type: ignore[attr-defined]
        obj.datatype = datatype  # type: ignore[attr-defined]
        return obj  # type: ignore[return-value]

    def __getitem__(self, key: int) -> int:
        if key == 0:
            return self.layer
        elif key == 1:
            return self.datatype

        else:
            raise ValueError(
                "LayerMap only has two values accessible like"
                " a list, layer == [0] and datatype == [1]"
            )

    def __len__(self) -> int:
        return 2

    def __iter__(self) -> Iterator[int]:
        yield from [self.layer, self.datatype]

    def __str__(self) -> str:
        return self.name


TT = TypeVar("TT", bound=kdb.Trans | kdb.DTrans | kdb.ICplxTrans | kdb.DCplxTrans)
TD = TypeVar("TD", bound=kdb.DTrans | kdb.DCplxTrans)
TI = TypeVar("TI", bound=kdb.Trans | kdb.ICplxTrans)
TS = TypeVar("TS", bound=kdb.Trans | kdb.DTrans)
TC = TypeVar("TC", bound=kdb.ICplxTrans | kdb.DCplxTrans)
FI = TypeVar("FI", bound=int | float)


class PortLike(ABCMeta, Generic[TT, FI]):

    yaml_tag: str
    name: str
    width: FI
    layer: int
    trans: TT
    port_type: str

    # def copy(self, trans: TI) -> "PortLike[TT, FI]":
    #     ...

    def move(
        self,
        origin: tuple[FI, FI],
        destination: tuple[FI, FI] = (cast(FI, 0), cast(FI, 0)),
    ) -> None:
        ...

    @property
    @abstractmethod
    def center(self) -> tuple[FI, FI]:
        ...

    @center.setter
    @abstractmethod
    def center(self, value: tuple[FI, FI]) -> None:
        ...

    @abstractmethod
    @property
    def x(self) -> FI:
        ...

    @abstractmethod
    @property
    def y(self) -> FI:
        ...

    @abstractmethod
    def hash(self) -> bytes:
        ...

    @abstractmethod
    def complex(self) -> bool:
        ...

    @abstractmethod
    def int_based(self) -> bool:
        ...

    @abstractmethod
    def dcplx_trans(self, dbu: float) -> kdb.DCplxTrans:
        ...

    def copy_cplx(self, trans: kdb.DCplxTrans, dbu: float) -> "DCplxPort":
        if self.int_based():
            return DCplxPort(
                width=self.width * dbu,
                layer=self.layer,
                name=self.name,
                port_type=self.port_type,
                trans=trans * self.dcplx_trans(dbu),
            )
        else:
            return DCplxPort(
                width=self.width,
                layer=self.layer,
                name=self.name,
                port_type=self.port_type,
                trans=trans * self.dcplx_trans(dbu),
            )

    @abstractmethod
    def copy(self) -> "PortLike[TT, FI]":
        ...


class IPortLike(PortLike[TI, int]):
    """Protocol for integer based ports"""

    @overload
    def __init__(
        self,
        *,
        name: str,
        trans: TI,
        width: int,
        layer: int,
        port_type: str = "optical",
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        *,
        name: Optional[str] = None,
        port: "IPortLike[TI]",
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        *,
        name: str,
        width: int,
        position: tuple[int, int],
        angle: int,
        layer: int,
        port_type: str = "optical",
        mirror_x: bool = False,
    ) -> None:
        ...

    def __init__(
        self,
        *,
        width: Optional[int] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[TI | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[int, int]] = None,
        mirror_x: bool = False,
        port: "Optional[IPortLike[TI]]" = None,
    ):
        ...

    def __repr__(self) -> str:
        return f"Port(\n    name: {self.name}\n    trans: {self.trans}\n    width: {self.width}\n    layer: {f'{self.layer} ({int(self.layer)})' if isinstance(self.layer, LayerEnum) else str(self.layer)}\n    port_type: {self.port_type}\n)"

    @property
    def position(self) -> tuple[int, int]:
        """Gives the x and y coordinates of the Port. This is info stored in the transformation of the port.

        Returns:
            position: `(self.trans.disp.x, self.trans.disp.y)`
        """
        return (self.trans.disp.x, self.trans.disp.y)

    @property
    def mirror(self) -> bool:
        """Flag to mirror the transformation. Mirroring is in increments of 45° planes.
        E.g. a rotation of 90° and mirror flag result in a mirroring on the 45° plane.
        """
        return self.trans.is_mirror()

    @property
    def x(self) -> int:
        """Convenience for :py:attr:`Port.trans.disp.x`"""
        return self.trans.disp.x

    @property
    def y(self) -> int:
        """Convenience for :py:attr:`Port.trans.disp.y`"""
        return self.trans.disp.y

    def hash(self) -> bytes:
        """Provides a hash function to provide a (hopefully) unique id of a port

        Returns:
            hash-digest: A byte representation from sha3_512()
        """
        h = sha3_512()
        h.update(self.name.encode("UTF-8"))
        h.update(self.trans.hash().to_bytes(8, "big"))
        h.update(self.width.to_bytes(8, "big"))
        h.update(self.port_type.encode("UTF-8"))
        h.update(self.layer.to_bytes(8, "big"))
        return h.digest()

    @classmethod
    def to_yaml(cls, representer, node):  # type: ignore
        """Internal function used by ruamel.yaml to convert Port to yaml"""
        return representer.represent_mapping(
            cls.yaml_tag,
            {
                "name": node.name,
                "width": node.width,
                "layer": node.layer,
                "port_type": node.port_type,
                "trans": node.trans.to_s(),
            },
        )

    @property
    def center(self) -> tuple[int, int]:
        """Returns port position for gdsfactory compatibility."""
        return self.position

    @center.setter
    def center(self, value: tuple[int, int]) -> None:
        self.trans.disp = kdb.Vector(*value)

    def int_based(self) -> bool:
        return True


class DPortLike(PortLike[TD, float]):
    """Protocol for floating number based ports"""

    @overload
    def __init__(
        self,
        *,
        name: str,
        trans: TD,
        width: float,
        layer: int,
        port_type: str = "optical",
    ) -> None:
        ...

    @overload
    def __init__(
        self,
        *,
        name: Optional[str] = None,
        port: "DPortLike[TD]",
    ) -> None:
        ...

    def __init__(
        self,
        *,
        width: Optional[float] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[TD | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[float, float]] = None,
        mirror_x: bool = False,
        port: "Optional[DPortLike[TD]]" = None,
    ):
        ...

    def __repr__(self) -> str:
        return f"Port(\n    name: {self.name}\n    trans: {self.trans}\n    width: {self.width}\n    layer: {f'{self.layer} ({int(self.layer)})' if isinstance(self.layer, LayerEnum) else str(self.layer)}\n    port_type: {self.port_type}\n)"

    @property
    def position(self) -> tuple[float, float]:
        """Gives the x and y coordinates of the Port. This is info stored in the transformation of the port.

        Returns:
            position: `(self.trans.disp.x, self.trans.disp.y)`
        """
        return (self.trans.disp.x, self.trans.disp.y)

    @property
    def mirror(self) -> bool:
        """Flag to mirror the transformation. Mirroring is in increments of 45° planes.
        E.g. a rotation of 90° and mirror flag result in a mirroring on the 45° plane.
        """
        return self.trans.is_mirror()

    @property
    def x(self) -> float:
        """Convenience for :py:attr:`Port.trans.disp.x`"""
        return self.trans.disp.x

    @property
    def y(self) -> float:
        """Convenience for :py:attr:`Port.trans.disp.y`"""
        return self.trans.disp.y

    def hash(self) -> bytes:
        """Provides a hash function to provide a (hopefully) unique id of a port

        Returns:
            hash-digest: A byte representation from sha3_512()
        """
        h = sha3_512()
        h.update(self.name.encode("UTF-8"))
        h.update(self.trans.hash().to_bytes(8, "big"))
        h.update(self.width.hex().encode("UTF-8"))
        h.update(self.port_type.encode("UTF-8"))
        h.update(self.layer.to_bytes(8, "big"))
        return h.digest()

    @classmethod
    def to_yaml(cls, representer, node):  # type: ignore
        """Internal function used by ruamel.yaml to convert Port to yaml"""
        return representer.represent_mapping(
            cls.yaml_tag,
            {
                "name": node.name,
                "width": node.width,
                "layer": node.layer,
                "port_type": node.port_type,
                "trans": node.trans.to_s(),
            },
        )

    @property
    def center(self) -> tuple[float, float]:
        """Returns port position for gdsfactory compatibility."""
        return self.position

    @center.setter
    def center(self, value: tuple[float, float]) -> None:
        self.trans.disp = kdb.DVector(*value)

    def int_based(self) -> bool:
        return False


class SPortLike(PortLike[TS, Any]):
    """Protocol for simple transformation based ports"""

    @property
    def angle(self) -> int:
        """Angle of the transformation. In the range of [0,1,2,3] which are increments in 90°. Not to be confused with `rot`
        of the transformation which keeps additional info about the mirror flag."""
        return self.trans.angle

    @property
    def orientation(self) -> float:
        """Returns orientation in degrees for gdsfactory compatibility."""
        return self.trans.angle * 90

    @orientation.setter
    def orientation(self, value: int) -> None:
        self.trans.angle = int(value // 90)

    def complex(self) -> bool:
        return False


class CPortLike(PortLike[TC, Any]):
    """Protocol for complex transformation based ports"""

    trans: TC

    @property
    def angle(self) -> float:
        """Angle of the transformation. In the range of [0,1,2,3] which are increments in 90°. Not to be confused with `rot`
        of the transformation which keeps additional info about the mirror flag."""
        return self.trans.angle

    @property
    def orientation(self) -> float:
        """Returns orientation in degrees for gdsfactory compatibility."""
        return self.trans.angle

    @orientation.setter
    def orientation(self, value: float) -> None:
        self.trans.angle = value

    def complex(self) -> bool:
        return True


class Port(IPortLike[kdb.Trans], SPortLike[kdb.Trans]):
    """A port is similar to a pin in electronics. In addition to the location and layer
    that defines a pin, a port also contains an orientation and a width. This can be fully represented with a transformation, integer and layer_index.
    """

    yaml_tag = "!Port"
    name: str
    width: int
    layer: int
    trans: kdb.Trans
    port_type: str

    def __init__(
        self,
        *,
        width: Optional[int] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[kdb.Trans | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[int, int]] = None,
        mirror_x: bool = False,
        port: Optional["Port"] = None,
    ):
        if port is not None:
            self.name = port.name if name is None else name
            self.trans = port.trans.dup()
            self.port_type = port.port_type
            self.layer = port.layer
            self.width = port.width
        elif name is None or width is None or layer is None:
            raise ValueError("name, width, layer must be given if the 'port is None'")
        else:
            self.name = name
            self.width = width
            self.layer = layer
            self.port_type = port_type
            if trans is not None:
                self.trans = (
                    kdb.Trans.from_s(trans) if isinstance(trans, str) else trans.dup()
                )
            elif angle is None or position is None:
                raise ValueError(
                    "angle and position must be given if creating a gdsfactory like port"
                )
            else:
                self.trans = kdb.Trans(angle, mirror_x, kdb.Vector(*position))

    def move(
        self, origin: tuple[int, int], destination: Optional[tuple[int, int]] = None
    ) -> None:
        """Convenience from the equivalent of gdsfactory. Moves the"""
        dest = kdb.Vector(*(origin if destination is None else destination))
        org = kdb.Vector(0, 0) if destination is None else kdb.Vector(*origin)

        self.trans = self.trans * kdb.Trans(dest - org)

    @classmethod
    def from_yaml(cls, constructor, node):  # type: ignore
        """Internal function used by the placer to convert yaml to a Port"""
        d = dict(constructor.construct_pairs(node))
        return cls(**d)

    def rotate(self, angle: int) -> None:
        """Rotate the Port

        Args:
            angle: The angle to rotate in increments of 90°
        """
        self.trans = self.trans * kdb.Trans(angle, False, 0, 0)

    def copy(self, trans: kdb.Trans = kdb.Trans.R0) -> "Port":
        """Get a copy of a port

        Args:
            trans: an optional transformation applied to the port to be copied

        Returns:
            port (:py:class:`Port`): a copy of the port
        """
        _trans = trans * self.trans
        return Port(
            name=self.name,
            trans=_trans,
            layer=self.layer,
            port_type=self.port_type,
            width=self.width,
        )

    def dcplx_trans(self, dbu: float) -> kdb.DCplxTrans:
        return kdb.DCplxTrans(self.trans.to_dtype(dbu))


class DPort(DPortLike[kdb.DTrans], SPortLike[kdb.DTrans]):
    """A port is similar to a pin in electronics. In addition to the location and layer
    that defines a pin, a port also contains an orientation and a width. This can be fully represented with a transformation, integer and layer_index.
    """

    yaml_tag = "!DPort"
    name: str
    width: float
    layer: int
    trans: kdb.DTrans
    port_type: str

    def __init__(
        self,
        *,
        width: Optional[float] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[kdb.DTrans | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[float, float]] = None,
        mirror_x: bool = False,
        port: Optional["DPort"] = None,
    ):
        if port is not None:
            self.name: str = port.name if name is None else name
            self.trans: kdb.DTrans = port.trans.dup()
            self.port_type: str = port.port_type
            self.layer: int = port.layer
            self.width: float = port.width
        elif name is None or width is None or layer is None:
            raise ValueError("name, width, layer must be given if the 'port is None'")
        else:
            self.name = name
            self.width = width
            self.layer = layer
            self.port_type = port_type
            if trans is not None:
                self.trans = (
                    kdb.DTrans.from_s(trans) if isinstance(trans, str) else trans.dup()
                )
            elif angle is None or position is None:
                raise ValueError(
                    "angle and position must be given if creating a gdsfactory like port"
                )
            else:
                self.trans = kdb.DTrans(angle, mirror_x, *position)

    def move(
        self,
        origin: tuple[float, float],
        destination: Optional[tuple[float, float]] = None,
    ) -> None:
        """Convenience from the equivalent of gdsfactory. Moves the"""
        dest = kdb.DVector(*(origin if destination is None else destination))
        org = kdb.DVector(0, 0) if destination is None else kdb.DVector(*origin)

        self.trans = self.trans * kdb.DTrans(dest - org)

    @classmethod
    def from_yaml(cls, constructor, node):  # type: ignore
        """Internal function used by the placer to convert yaml to a Port"""
        d = dict(constructor.construct_pairs(node))
        return cls(**d)

    def rotate(self, angle: int) -> None:
        """Rotate the Port

        Args:
            angle: The angle to rotate in increments of 90°
        """
        self.trans = self.trans * kdb.DTrans(angle, False, 0, 0)

    def copy(self, trans: kdb.DTrans = kdb.DTrans.R0) -> "DPort":
        """Get a copy of a port

        Args:
            trans: an optional transformation applied to the port to be copied

        Returns:
            port (:py:class:`Port`): a copy of the port
        """
        _trans = trans * self.trans
        return DPort(
            name=self.name,
            trans=_trans,
            layer=self.layer,
            port_type=self.port_type,
            width=self.width,
        )

    def dcplx_trans(self, dbu: float) -> kdb.DCplxTrans:
        return kdb.DCplxTrans(self.trans)


class ICplxPort(IPortLike[kdb.ICplxTrans], CPortLike[kdb.ICplxTrans]):
    """A port is similar to a pin in electronics. In addition to the location and layer
    that defines a pin, a port also contains an orientation and a width. This can be fully represented with a transformation, integer and layer_index.
    """

    yaml_tag = "!ICplxPort"
    name: str
    width: int
    layer: int
    trans: kdb.ICplxTrans
    port_type: str

    def __init__(
        self,
        *,
        width: Optional[int] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[kdb.ICplxTrans | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[int, int]] = None,
        mirror_x: bool = False,
        port: Optional["ICplxPort"] = None,
    ):
        if port is not None:
            self.name = port.name if name is None else name
            self.trans = port.trans.dup()
            self.port_type = port.port_type
            self.layer = port.layer
            self.width = port.width
        elif name is None or width is None or layer is None:
            raise ValueError("name, width, layer must be given if the 'port is None'")
        else:
            self.name = name
            self.width = width
            self.layer = layer
            self.port_type = port_type
            if trans is not None:
                self.trans = (
                    kdb.ICplxTrans.from_s(trans)
                    if isinstance(trans, str)
                    else trans.dup()
                )
            elif angle is None or position is None:
                raise ValueError(
                    "angle and position must be given if creating a gdsfactory like port"
                )
            else:
                self.trans = kdb.ICplxTrans(1, angle, mirror_x, *position)

    def move(
        self,
        origin: tuple[int, int],
        destination: Optional[tuple[int, int]] = None,
    ) -> None:
        """Convenience from the equivalent of gdsfactory. Moves the"""
        dest = kdb.Vector(*(origin if destination is None else destination))
        org = kdb.Vector(0, 0) if destination is None else kdb.Vector(*origin)

        self.trans = self.trans * kdb.ICplxTrans(dest - org)

    @classmethod
    def from_yaml(cls, constructor, node):  # type: ignore
        """Internal function used by the placer to convert yaml to a Port"""
        d = dict(constructor.construct_pairs(node))
        return cls(**d)

    def rotate(self, angle: int) -> None:
        """Rotate the Port

        Args:
            angle: The angle to rotate in increments of 90°
        """
        self.trans = self.trans * kdb.ICplxTrans(1, angle, False, 0, 0)

    def copy(self, trans: kdb.ICplxTrans = kdb.ICplxTrans.R0) -> "ICplxPort":
        """Get a copy of a port

        Args:
            trans: an optional transformation applied to the port to be copied

        Returns:
            port (:py:class:`Port`): a copy of the port
        """
        _trans = trans * self.trans
        return ICplxPort(
            name=self.name,
            trans=_trans,
            layer=self.layer,
            port_type=self.port_type,
            width=self.width,
        )

    def dcplx_trans(self, dbu: float) -> kdb.DCplxTrans:
        return self.trans.to_itrans(dbu)


class DCplxPort(DPortLike[kdb.DCplxTrans], CPortLike[kdb.DCplxTrans]):
    """A port is similar to a pin in electronics. In addition to the location and layer
    that defines a pin, a port also contains an orientation and a width. This can be fully represented with a transformation, integer and layer_index.
    """

    yaml_tag = "!DCplxPort"
    name: str
    width: float
    layer: int
    trans: kdb.DCplxTrans
    port_type: str

    def __init__(
        self,
        *,
        width: Optional[float] = None,
        layer: Optional[int] = None,
        name: Optional[str] = None,
        port_type: str = "optical",
        trans: Optional[kdb.DCplxTrans | str] = None,
        angle: Optional[int] = None,
        position: Optional[tuple[float, float]] = None,
        mirror_x: bool = False,
        port: Optional["DCplxPort"] = None,
    ):
        if port is not None:
            self.name: str = port.name if name is None else name
            self.trans: kdb.DCplxTrans = port.trans.dup()
            self.port_type: str = port.port_type
            self.layer: int = port.layer
            self.width: float = port.width
        elif name is None or width is None or layer is None:
            raise ValueError("name, width, layer must be given if the 'port is None'")
        else:
            self.name = name
            self.width = width
            self.layer = layer
            self.port_type = port_type
            if trans is not None:
                self.trans = (
                    kdb.DCplxTrans.from_s(trans)
                    if isinstance(trans, str)
                    else trans.dup()
                )
            elif angle is None or position is None:
                raise ValueError(
                    "angle and position must be given if creating a gdsfactory like port"
                )
            else:
                self.trans = kdb.DCplxTrans(1, angle, mirror_x, *position)

    def move(
        self,
        origin: tuple[float, float],
        destination: Optional[tuple[float, float]] = None,
    ) -> None:
        """Convenience from the equivalent of gdsfactory. Moves the"""
        dest = kdb.DVector(*(origin if destination is None else destination))
        org = kdb.DVector(0, 0) if destination is None else kdb.DVector(*origin)

        self.trans = self.trans * kdb.DCplxTrans(dest - org)

    @classmethod
    def from_yaml(cls, constructor, node):  # type: ignore
        """Internal function used by the placer to convert yaml to a Port"""
        d = dict(constructor.construct_pairs(node))
        return cls(**d)

    def rotate(self, angle: int) -> None:
        """Rotate the Port

        Args:
            angle: The angle to rotate in increments of 90°
        """
        self.trans = self.trans * kdb.DCplxTrans(1, angle, False, 0, 0)

    def copy(self, trans: kdb.DCplxTrans = kdb.DCplxTrans.R0) -> "DCplxPort":
        """Get a copy of a port

        Args:
            trans: an optional transformation applied to the port to be copied

        Returns:
            port (:py:class:`Port`): a copy of the port
        """
        _trans = trans * self.trans
        return DCplxPort(
            name=self.name,
            trans=_trans,
            layer=self.layer,
            port_type=self.port_type,
            width=self.width,
        )

    def dcplx_trans(self, dbu: float) -> kdb.DCplxTrans:
        return self.trans.dup()


class KCell:
    """Derived from :py:class:`klayout.db.Cell`. Additionally to a standard cell, this one will keep track of :py:class:`Port` and allow to store metadata in a dictionary

    Attributes:
        ports (:py:class:`Ports`):  Contains all the ports of the cell and makes them accessible
        insts (:py:class:`list`[:py:class:`Instance`]): All instances intantiated in this KCell
        settings (:py:class:`dict`): Dictionary containing additional metadata of the KCell. Can be autopopulated by :py:func:`autocell`
        _kdb_cell (:py:class:`klayout.db.Cell`): Internal reference to the :py:class:`klayout.db.Cell` object. Not intended for direct access
    """

    yaml_tag = "!KCell"

    def __init__(
        self,
        name: Optional[str] = None,
        library: KLib = library,
        kdb_cell: Optional[kdb.Cell] = None,
    ) -> None:
        _name = name if name is not None else "Unnamed_"
        self.library: KLib = library
        if kdb_cell is None:
            self._kdb_cell: kdb.Cell = self.library.create_cell(self, _name)
            if name is None:
                self.name = f"Unnamed_{self.cell_index()}"
        else:
            self._kdb_cell = kdb_cell
            self.library.register_cell(self)
            self.name = kdb_cell.name if name is None else name
        self.ports: Ports = Ports()
        self.insts: list[Instance] = []
        self.settings: dict[str, Any] = {}
        self._locked = False
        self.info: dict[str, Any] = {}

    def copy(self) -> "KCell":
        """Copy the full cell

        Returns:
            cell: exact copy of the current cell
        """
        kdb_copy = self._kdb_cell.dup()
        c = KCell(library=self.library, kdb_cell=kdb_copy)
        c.ports = self.ports
        for inst in self.insts:
            c.create_inst(inst.cell, inst.instance.trans)
        c._locked = False
        return c

    @property
    def ports(self) -> "Ports":
        return self._ports

    @ports.setter
    def ports(self, new_ports: "InstancePorts | Ports") -> None:
        self._ports = new_ports.copy()

    @property
    def name(self) -> str:
        """Returns the KLayout Cell name

        Rerturns:
            name: `klayout.db.Cell.name`
        """
        return self._kdb_cell.name

    @name.setter
    def name(self, new_name: str) -> None:
        """Setter for the Name

        This will set the name in the KCell library :py:attr:`~library`
        Also updates the underlying KLayout Cell

        Args:
            new_name: The new name of the cell
        """
        name = self._kdb_cell.name
        self._kdb_cell.name = new_name
        self.library.update_cell_name(name, new_name)

    @overload
    def create_port(
        self,
        *,
        name: str,
        trans: kdb.Trans,
        width: int,
        layer: int | LayerEnum,
        port_type: str = "optical",
    ) -> None:
        ...

    @overload
    def create_port(
        self,
        *,
        name: Optional[str] = None,
        port: Port,
    ) -> None:
        ...

    @overload
    def create_port(
        self,
        *,
        name: str,
        width: int,
        position: tuple[int, int],
        angle: int,
        layer: int | LayerEnum,
        port_type: str = "optical",
        mirror_x: bool = False,
    ) -> None:
        ...

    def create_port(self, **kwargs: Any) -> None:
        """Create a new port. Equivalent to :py:attr:`~add_port(Port(...))`"""
        self.ports.create_port(**kwargs)

    def add_port(self, port: PortLike[TT, FI], name: Optional[str] = None) -> None:
        """Add an existing port. E.g. from an instance to propagate the port

        Args:
            port: The port to add. Port should either be a :py:class:`~Port`, or will be converted to an integer based port with 90° increment
            name: Overwrite the name of the port
        """

        if isinstance(port, Port):
            self.ports.add_port(port=port, name=name)
        else:
            warnings.warn(
                f"Port {str(port)} is not an integer based port, converting to integer based"
            )

            if port.complex():
                strans = port.trans.s_trans()  # type: ignore[union-attr]
            else:
                strans = port.trans.dup()

            if port.int_based():
                trans = strans
            else:
                trans = strans.to_itype(self.library.dbu)  # type: ignore[union-attr]

            _port = Port(
                name=port.name,
                width=port.width  # type: ignore[arg-type]
                if port.int_based()
                else int(port.width / self.library.dbu),
                trans=trans,  # type: ignore[arg-type]
                port_type=port.port_type,
                layer=port.layer,
            )
            self.ports.add_port(port=_port, name=name)

    def create_inst(self, cell: "KCell", trans: kdb.Trans = kdb.Trans()) -> "Instance":
        """Add an instance of another KCell

        Args:
            cell: The cell to be added
            trans: The transformation applied to the reference

        Returns:
            :py:class:`~Instance`: The created instance
        """
        ca = self.insert(kdb.CellInstArray(cell._kdb_cell.cell_index(), trans))
        inst = Instance(cell, ca)
        self.insts.append(inst)
        return inst

    def layer(self, *args: Any, **kwargs: Any) -> int:
        """Get the layer info, convenience for klayout.db.Layout.layer"""
        return self.library.layer(*args, **kwargs)

    def __lshift__(self, cell: "KCell") -> "Instance":
        """Convenience function for :py:attr:"~create_inst(cell)`

        Args:
            cell: The cell to be added as an instance
        """
        return self.create_inst(cell)

    def __getattribute__(self, attr_name: str) -> Any:
        """Overwrite the standard getattribute. If the attribute is not set by KCell, go look in the klayout.db.Cell object"""
        if attr_name in {"name"}:
            return self.__getattr__(attr_name)
        else:
            return super().__getattribute__(attr_name)

    def _get_attr(self, attr_name: str) -> Any:
        """look in the klayout.db.Cell for an attribute, used by settattr to set the name"""
        return super().__getattribute__(attr_name)

    def __getattr__(self, attr_name: str) -> Any:
        """Look in the klayout.db.Cell for attributes"""
        return kdb.Cell.__getattribute__(self._kdb_cell, attr_name)

    def __setattr__(self, attr_name: str, attr_value: Any) -> None:
        """Custom set attribute function. Name and klayout.db.Cell attribute have to be set manually

        Everything else look first in the klayout.db.Cell whether the attribute exists, otherwise set it in the KCell
        """
        if attr_name in {"_kdb_cell", "name"}:
            super().__setattr__(attr_name, attr_value)
        try:
            kdb.Cell.__setattr__(self._get_attr("_kdb_cell"), attr_name, attr_value)
        except AttributeError as a:
            super().__setattr__(attr_name, attr_value)

    def hash(self) -> bytes:
        """Provide a unique hash of the cell"""
        h = sha3_512()
        h.update(self.name.encode("ascii", "ignore"))

        for l in self.layout().layer_indexes():
            for shape in self.shapes(l).each(kdb.Shapes.SRegions):
                h.update(shape.polygon.hash().to_bytes(8, "big"))
            for shape in self.shapes(l).each(kdb.Shapes.STexts):
                h.update(shape.text.hash().to_bytes(8, "big"))
        port_hashs = list(sorted(p.hash() for p in self.ports._ports))
        for _hash in port_hashs:
            h.update(_hash)
        insts_hashs = list(sorted(inst.hash() for inst in self.insts))
        return h.digest()

    def autorename_ports(
        self, rename_func: Optional[Callable[..., None]] = None
    ) -> None:
        """Rename the ports with the schema angle -> "NSWE" and sort by x and y

        Args:
            rename_func: Function that takes Iterable[Port] and renames them. This can of course contain a filter and only rename some of the ports
        """

        if rename_func is None:
            self.library.rename_function(self.ports._ports)
        else:
            rename_func(self.ports._ports)

    def flatten(self, prune: bool = True, merge: bool = True) -> None:
        """Flatten the cell. Pruning will delete the klayout.db.Cell if unused, but might cause artifacts at the moment

        Args:
            prune: Delete unused child cells if they aren't used in any other KCell
            merge: Merge the shapes on all layers"""
        self._kdb_cell.flatten(False)  # prune)
        self.insts = []

        if merge:
            for layer in self.layout().layer_indexes():
                reg = kdb.Region(self.begin_shapes_rec(layer))
                reg.merge()
                self.clear(layer)
                self.shapes(layer).insert(reg)

    def draw_ports(self) -> None:
        """Draw all the ports on their respective :py:attr:`Port.layer`:"""
        for port in self.ports._ports:

            if isinstance(port, IPortLike):
                w = port.width
                poly = kdb.Polygon(
                    [kdb.Point(0, -w // 2), kdb.Point(0, w // 2), kdb.Point(w // 2, 0)]
                )
                self.shapes(port.layer).insert(poly.transformed(port.trans))
                self.shapes(port.layer).insert(
                    kdb.Text(port.name, kdb.Trans.R0).transformed(port.trans)
                )
            elif isinstance(port, DPortLike):
                wd = port.width
                dpoly = kdb.DPolygon(
                    [
                        kdb.DPoint(0, -wd / 2),
                        kdb.DPoint(0, wd / 2),
                        kdb.DPoint(wd / 2, 0),
                    ]
                )
                self.shapes(port.layer).insert(dpoly.transformed(port.trans))
                self.shapes(port.layer).insert(
                    kdb.DText(port.name, kdb.DTrans.R0).transformed(port.trans)
                )

    def write(
        self, filename: str | Path, save_options: kdb.SaveLayoutOptions = default_save()
    ) -> None:
        return self._kdb_cell.write(str(filename), save_options)

    @classmethod
    def to_yaml(cls, representer, node):  # type: ignore
        """Internal function to convert the cell to yaml"""
        d = {
            "name": node.name,
            "ports": node.ports,  # Ports.to_yaml(representer, node.ports),
        }

        insts = [
            {"cellname": inst.cell.name, "trans": inst.instance.trans.to_s()}
            for inst in node.insts
        ]
        shapes = {
            node.layout()
            .get_info(layer)
            .to_s(): [shape.to_s() for shape in node.shapes(layer).each()]
            for layer in node.layout().layer_indexes()
            if not node.shapes(layer).is_empty()
        }

        if insts:
            d["insts"] = insts
        if shapes:
            d["shapes"] = shapes
        if len(node.settings) > 0:
            d["settings"] = node.settings
        return representer.represent_mapping(cls.yaml_tag, d)

    @classmethod
    def from_yaml(cls, constructor, node, verbose=False):  # type: ignore
        """Internal function used by the placer to convert yaml to a KCell"""
        d = ruamel.yaml.constructor.SafeConstructor.construct_mapping(
            constructor,
            node,
            deep=True,
        )
        cell = cls(d["name"])
        if verbose:
            print(f"Building {d['name']}")
        cell.ports = d.get("ports", Ports([]))
        cell.settings = d.get("settings", {})
        for inst in d.get("insts", []):
            if "cellname" in inst:
                _cell = cell.library.kcells[inst["cellname"]]
            elif "cellfunction" in inst:
                module_name, fname = inst["cellfunction"].rsplit(".", 1)
                module = importlib.import_module(module_name)
                cellf = getattr(module, fname)
                _cell = cellf(**inst["settings"])
                del module
            else:
                raise NotImplementedError(
                    'To define an instance, either a "cellfunction" or a "cellname" needs to be defined'
                )
            t = inst.get("trans", {})
            if isinstance(t, str):
                cell.create_inst(
                    _cell,
                    kdb.Trans.from_s(inst["trans"]),
                )
            else:
                angle = t.get("angle", 0)
                mirror = t.get("mirror", False)

                kinst = cell.create_inst(
                    _cell,
                    kdb.Trans(angle, mirror, 0, 0),
                )

                x0_yml = t.get("x0", DEFAULT_TRANS["x0"])
                y0_yml = t.get("y0", DEFAULT_TRANS["y0"])
                x_yml = t.get("x", DEFAULT_TRANS["x"])
                y_yml = t.get("y", DEFAULT_TRANS["y"])
                margin = t.get("margin", DEFAULT_TRANS["margin"])
                margin_x = margin.get("x", DEFAULT_TRANS["margin"]["x"])  # type: ignore[index]
                margin_y = margin.get("y", DEFAULT_TRANS["margin"]["y"])  # type: ignore[index]
                margin_x0 = margin.get("x0", DEFAULT_TRANS["margin"]["x0"])  # type: ignore[index]
                margin_y0 = margin.get("y0", DEFAULT_TRANS["margin"]["y0"])  # type: ignore[index]
                ref_yml = t.get("ref", DEFAULT_TRANS["ref"])
                if isinstance(ref_yml, str):
                    for i in reversed(cell.insts):
                        if i.cell.name == ref_yml:
                            ref = i
                            break
                    else:
                        IndexError(f"No instance with cell name: <{ref_yml}> found")
                elif isinstance(ref_yml, int) and len(cell.insts) > 1:
                    ref = cell.insts[ref_yml]

                # margins for x0/y0 need to be in with opposite sign of x/y due to them being subtracted later
                # x0
                match x0_yml:
                    case "W":
                        x0 = kinst.bbox().left - margin_x0
                    case "E":
                        x0 = kinst.bbox().right + margin_x0
                    case _:
                        if isinstance(x0_yml, int):
                            x0 = x0_yml
                        else:
                            NotImplementedError("unknown format for x0")
                # y0
                match y0_yml:
                    case "S":
                        y0 = kinst.bbox().bottom - margin_y0
                    case "N":
                        y0 = kinst.bbox().top + margin_y0
                    case _:
                        if isinstance(y0_yml, int):
                            y0 = y0_yml
                        else:
                            NotImplementedError("unknown format for y0")
                # x
                match x_yml:
                    case "W":
                        if len(cell.insts) > 1:
                            x = ref.bbox().left
                            if x_yml != x0_yml:
                                x -= margin_x
                        else:
                            x = margin_x
                    case "E":
                        if len(cell.insts) > 1:
                            x = ref.bbox().right
                            if x_yml != x0_yml:
                                x += margin_x
                        else:
                            x = margin_x
                    case _:
                        if isinstance(x_yml, int):
                            x = x_yml
                        else:
                            NotImplementedError("unknown format for x")
                # y
                match y_yml:
                    case "S":
                        if len(cell.insts) > 1:
                            y = ref.bbox().bottom
                            if y_yml != y0_yml:
                                y -= margin_y
                        else:
                            y = margin_y
                    case "N":
                        if len(cell.insts) > 1:
                            y = ref.bbox().top
                            if y_yml != y0_yml:
                                y += margin_y
                        else:
                            y = margin_y
                    case _:
                        if isinstance(y_yml, int):
                            y = y_yml
                        else:
                            NotImplementedError("unknown format for y")
                kinst.transform(kdb.Trans(0, False, x - x0, y - y0))

        type_to_class = {
            "box": kdb.Box.from_s,
            "polygon": kdb.Polygon.from_s,
            "edge": kdb.Edge.from_s,
            "text": kdb.Text.from_s,
            "dbox": kdb.DBox.from_s,
            "dpolygon": kdb.DPolygon.from_s,
            "dedge": kdb.DEdge.from_s,
            "dtext": kdb.DText.from_s,
        }

        for layer, shapes in dict(d.get("shapes", {})).items():
            linfo = kdb.LayerInfo.from_string(layer)
            for shape in shapes:
                shapetype, shapestring = shape.split(" ", 1)
                cell.shapes(cell.layout().layer(linfo)).insert(
                    type_to_class[shapetype](shapestring)
                )

        return cell


class Instance:
    """An Instance of a KCell. An Instance is a reference to a KCell with a transformation

    Attributes:
        cell: The KCell that is referenced
        instance: The internal klayout.db.Instance reference
        ports: Transformed ports of the KCell"""

    yaml_tag = "!Instance"

    def __init__(self, cell: KCell, reference: kdb.Instance) -> None:
        self.cell = cell
        self.instance = reference
        self.ports = InstancePorts(self)

    def hash(self) -> bytes:
        h = sha3_512()
        h.update(self.cell.hash())
        h.update(self.instance.trans.hash().to_bytes(8, "big"))
        return h.digest()

    @overload
    def connect(self, portname: str, other: Port, *, mirror: bool = False) -> None:
        ...

    @overload
    def connect(
        self,
        portname: str,
        other: "Instance",
        other_port_name: str,
        *,
        mirror: bool = False,
    ) -> None:
        ...

    def connect(
        self,
        portname: str,
        other: "Instance | Port | DCplxPort",
        other_port_name: Optional[str] = None,
        *,
        mirror: bool = False,
        allow_width_mismatch: bool = False,
        allow_layer_mismatch: bool = False,
        allow_type_mismatch: bool = False,
    ) -> None:
        """Function to allow to transform this instance so that a port of this instance is connected (same position with 180° turn) to another instance.

        Args:
            portname: The name of the port of this instance to be connected
            other_instance: The other instance or a port
            other_port_name: The name of the other port. Ignored if :py:attr:`~other_instance` is a port.
            mirror: Instead of applying klayout.db.Trans.R180 as a connection transformation, use klayout.db.Trans.M90, which effectively means this instance will be mirrored and connected.
        """
        if isinstance(other, Instance):
            if other_port_name is None:
                raise ValueError(
                    "portname cannot be None if an Instance Object is given. For complex connections (non-90 degree and floating point ports) use connect_cplx instead"
                )
            op = other.ports[other_port_name]
        elif isinstance(other, Port):
            op = other
        else:
            raise ValueError("other_instance must be of type Instance or Port")
        p = self.cell.ports[portname]
        if p.width != op.width and not allow_width_mismatch:
            raise PortWidthMismatch(
                self,
                other,
                p,
                op,
            )
        elif int(p.layer) != int(op.layer) and not allow_layer_mismatch:
            raise PortLayerMismatch(self.cell.library, self, other, p, op)
        elif p.port_type != op.port_type and not allow_type_mismatch:
            raise PortTypeMismatch(self, other, p, op)
        else:

            if is_simple_port(op):
                conn_trans = kdb.Trans.M90 if mirror else kdb.Trans.R180
                self.instance.trans = op.trans * conn_trans * p.trans.inverted()
            else:
                if isinstance(op.trans, DPort):
                    d_conn_trans = kdb.DTrans.R180
                    d_p_trans = p.trans.to_dtype(self.cell.library.dbu).inverted()
                    self.instance.dtrans = op.trans, *d_conn_trans * d_p_trans
                elif isinstance(op.trans, ICplxPort):
                    icplx_conn_trans = kdb.ICplxTrans.R180
                    i_p_trans = kdb.ICplxTrans(p.trans).inverted()
                    self.instance.cplx_trans = op.trans * icplx_conn_trans * i_p_trans
                elif isinstance(op.trans, DCplxPort):
                    d_cplx_conn_trans = kdb.DCplxTrans.R180
                    d_p_trans = kdb.DCplxTrans(
                        p.trans.to_dtype(self.cell.library.dbu)
                    ).inverted()
                    self.instance.dcplx_trans = op.trans * d_cplx_conn_trans * d_p_trans

    def connect_cplx(
        self,
        portname: str,
        other: "Instance|PortLike[TT, FI]",
        other_port_name: Optional[str] = None,
        *,
        mirror: bool = False,
        allow_width_mismatch: bool = False,
        allow_layer_mismatch: bool = False,
        allow_type_mismatch: bool = False,
    ) -> None:

        if isinstance(other, Instance):
            if other_port_name is None:
                raise ValueError(
                    "portname cannot be None if an Instance Object is given"
                )
            op = other.ports[other_port_name]
        elif isinstance(other, (Port, DPort, ICplxPort, DCplxPort)):
            op = other
        else:
            raise ValueError("other_instance must be of type Instance or Port")
        p = self.cell.ports[portname]
        if p.width != op.width and not allow_width_mismatch:
            if p.int_based() == op.int_based():
                raise PortWidthMismatch(
                    self,
                    other,
                    p,
                    op,
                )
            w1 = p.width * self.cell.library.dbu if p.int_based() else p.width
            w2 = op.width * self.cell.library.dbu if op.int_based() else op.width
            if w1 != w2:

                raise PortWidthMismatch(
                    self,
                    other,
                    p,
                    op,
                )
        if int(p.layer) != int(op.layer) and not allow_layer_mismatch:
            raise PortLayerMismatch(self.cell.library, self, other, p, op)
        if p.port_type != op.port_type and not allow_type_mismatch:
            raise PortTypeMismatch(self, other, p, op)
        # reset the transformation
        self.trans = kdb.Trans.R0
        # apply the transformations piece by piece
        self.transform(op.trans)
        self.transform(kdb.Trans.M90 if mirror else kdb.Trans.R180)
        self.transform(p.trans.inverted())

    def __getattribute__(self, attr_name: str) -> Any:
        return super().__getattribute__(attr_name)

    def _get_attr(self, attr_name: str) -> Any:
        return super().__getattribute__(attr_name)

    def __getattr__(self, attr_name: str) -> Any:
        return kdb.Instance.__getattribute__(self.instance, attr_name)

    def __setattr__(self, attr_name: str, attr_value: Any) -> None:
        if attr_name == "instance":
            super().__setattr__(attr_name, attr_value)
        try:
            kdb.Instance.__setattr__(self._get_attr("instance"), attr_name, attr_value)
        except AttributeError as a:
            super().__setattr__(attr_name, attr_value)

    @classmethod
    def to_yaml(cls, representer, node):  # type: ignore
        d = {"cellname": node.cell.name, "trans": node.instance.trans}
        return representer.represent_mapping(cls.yaml_tag, d)


class Ports:
    """A list of ports. It is not a traditional dictionary. Elements can be retrieved as in a tradional dictionary. But to keep tabs on names etc, the ports are stored as a list

    Attributes:
        _ports: Internal storage of the ports. Normally ports should be retrieved with :py:func:`__getitem__` or with :py:func:`~get_all`
    """

    yaml_tag = "!Ports"

    def __init__(self, ports: list[Port] = []) -> None:
        """Constructor"""
        self._ports = list(ports)

    def copy(self) -> "Ports":
        """Get a copy of each port"""
        return Ports([p.copy() for p in self._ports])

    def contains(self, port: Port) -> bool:
        """Check whether a port is already in the list"""
        return port.hash() in [v.hash() for v in self._ports]

    def each(self) -> Iterator[Port]:
        yield from self._ports

    def add_port(self, port: Port, name: Optional[str] = None) -> None:
        """Add a port object

        Args:
            port: The port to add
            name: Overwrite the name of the port
        """
        _port = port.copy()
        if name is not None:
            _port.name = name
        if self.get_all().get(_port.name, None) is not None:
            raise ValueError("Port hase already been added to this cell")
        self._ports.append(_port)

    @overload
    def create_port(
        self,
        *,
        name: str,
        trans: kdb.Trans,
        width: int,
        layer: int,
        port_type: str = "optical",
    ) -> Port:
        ...

    @overload
    def create_port(
        self,
        *,
        name: str,
        width: int,
        layer: int,
        position: tuple[int, int],
        angle: int,
        port_type: str = "optical",
    ) -> Port:
        ...

    def create_port(
        self,
        *,
        name: str,
        width: int,
        layer: int,
        port_type: str = "optical",
        trans: Optional[kdb.Trans] = None,
        position: Optional[tuple[int, int]] = None,
        angle: Optional[int] = None,
        mirror_x: bool = False,
    ) -> Port:
        """Create a new port in the list"""

        if trans is not None:
            port = Port(
                name=name, trans=trans, width=width, layer=layer, port_type=port_type
            )
        elif angle is not None and position is not None:
            port = Port(
                name=name,
                width=width,
                layer=layer,
                port_type=port_type,
                angle=angle,
                position=position,
                mirror_x=mirror_x,
            )
        else:
            raise ValueError(
                f"You need to define trans {trans} or angle {angle} and position {position}"
            )

        self._ports.append(port)
        return port

    def get_all(self) -> dict[str, Port]:
        """Get all ports in a dictionary with names as keys"""
        return {v.name: v for v in self._ports}

    def __getitem__(self, key: str) -> Port:
        """Get a specific port by name"""
        try:
            return next(filter(lambda port: port.name == key, self._ports))
        except StopIteration:
            raise StopIteration(
                f"{key} is not a port. Available ports: {[v.name for v in self._ports]}"
            )

    def hash(self) -> bytes:
        """Get a hash of the port to compare"""
        h = sha3_512()
        for port in sorted(
            sorted(self._ports, key=lambda port: port.name), key=lambda port: hash(port)
        ):
            h.update(port.name.encode("UTF-8"))
            h.update(port.trans.hash().to_bytes(8, "big"))
            h.update(port.width.to_bytes(8, "big"))
            h.update(port.port_type.encode("UTF-8"))
            h.update(port.port_type.encode("UTF-8"))

        return h.digest()

    def __repr__(self) -> str:
        return repr({v.name: v for v in self._ports})

    @classmethod
    def to_yaml(cls, representer, node):  # type: ignore[no-untyped-def]
        return representer.represent_sequence(
            cls.yaml_tag,
            node._ports,  # [Port.to_yaml(representer, p) for p in node._ports]
        )

    @classmethod
    def from_yaml(cls, constructor, node):  # type: ignore[no-untyped-def]

        return cls(constructor.construct_sequence(node))


class InstancePorts:
    def __init__(self, instance: Instance) -> None:
        self.cell_ports = instance.cell.ports
        self.instance = instance

    def __getitem__(self, key: str) -> Port | DCplxPort:
        p = self.cell_ports[key]
        if not (self.instance.instance.is_complex()):
            return p.copy(trans=self.instance.trans)
        else:
            return p.copy_cplx(
                trans=self.instance.instance.dcplx_trans,
                dbu=self.instance.cell.library.dbu,
            )

    def __repr__(self) -> str:
        return repr({v: self.__getitem__(v) for v in self.cell_ports.get_all().keys()})

    def get_all(self) -> dict[str, Port | DCplxPort]:
        return {v: self.__getitem__(v) for v in self.cell_ports.get_all().keys()}

    def copy(self) -> Ports:
        if not self.instance.instance.is_complex():

            return Ports(
                [
                    port.copy(trans=self.instance.trans)
                    for port in self.cell_ports._ports
                ]
            )
        else:
            raise AttributeError(
                f"The instance is a complex instance, cannot copy the port collection of a complex instance DCplxTrans={self.instance.instance.dcplx_trans}"
            )


@overload
def autocell(_func: Callable[KCellParams, KCell], /) -> Callable[KCellParams, KCell]:
    ...


@overload
def autocell(
    *,
    set_settings: bool = True,
    set_name: bool = True,
    maxsize: int = 512,
) -> Callable[[Callable[KCellParams, KCell]], Callable[KCellParams, KCell]]:
    ...


def autocell(
    _func: Optional[Callable[KCellParams, KCell]] = None,
    /,
    *,
    set_settings: bool = True,
    set_name: bool = True,
    maxsize: int = 512,
) -> Callable[KCellParams, KCell] | Callable[
    [Callable[KCellParams, KCell]], Callable[KCellParams, KCell]
]:
    """Decorator to cache and auto name the celll. This will use :py:func:`functools.cache` to cache the function call.
    Additionally, if enabled this will set the name and from the args/kwargs of the function and also paste them into a settings dictionary of the :py:class:`~KCell`

    Args:
        set_settings: Copy the args & kwargs into the settings dictionary
        set_name: Auto create the name of the cell to the functionname plus a string created from the args/kwargs
        maxsize: maximum size of cache, cell parameter sets will be evicted if the cell function is called with more different
        parameter sets than there are spaces in the cache, in case there are cell calls with existing parameter set calls
    """

    def decorator_autocell(
        f: Callable[KCellParams, KCell]
    ) -> Callable[KCellParams, KCell]:
        sig = signature(f)

        cache = KCellCache(maxsize)

        @functools.wraps(f)
        def wrapper_autocell(
            *args: KCellParams.args, **kwargs: KCellParams.kwargs
        ) -> KCell:
            params: dict[str, KCellParams.args] = {
                p.name: p.default for k, p in sig.parameters.items()
            }
            arg_par = list(sig.parameters.items())[: len(args)]
            for i, (k, v) in enumerate(arg_par):
                params[k] = args[i]
            params.update(kwargs)

            for key, value in params.items():
                if isinstance(value, dict):
                    params[key] = dict_to_frozen_set(value)

            @cached(cache=cache)
            @functools.wraps(f)
            def wrapped_cell(
                **params: KCellParams.args | KCellParams.kwargs,
            ) -> KCell:
                for key, value in params.items():
                    if isinstance(value, frozenset):
                        params[key] = frozenset_to_dict(value)
                cell = f(**params)
                if cell._locked:
                    cell = cell.copy()
                if set_name:
                    name = get_component_name(f.__name__, **params)
                    cell.name = name
                if set_settings:
                    cell.settings.update(params)

                i = 0
                for name, setting in cell.settings.items():
                    while cell.property(i) is not None:
                        i += 1
                    if isinstance(setting, KCell):
                        cell.set_property(i, f"{name}: {setting.name}")
                    else:
                        cell.set_property(i, f"{name}: {str(setting)}")
                    i += 1
                cell._locked = True
                return cell

            return wrapped_cell(**params)

        return wrapper_autocell

    if _func is None:

        return decorator_autocell
    else:
        return decorator_autocell(_func)


def dict_to_frozen_set(d: dict[str, Any]) -> frozenset[tuple[str, Any]]:
    return frozenset(d.items())


def frozenset_to_dict(fs: frozenset[tuple[str, Any]]) -> dict[str, Any]:
    return dict(fs)


def cell(
    _func: Optional[Callable[..., KCell]] = None,
    *,
    set_settings: bool = True,
    maxsize: int = 512,
) -> Callable[KCellParams, KCell] | Callable[
    [Callable[KCellParams, KCell]], Callable[KCellParams, KCell]
]:
    """Convenience alias for :py:func:`~autocell` with `(set_name=False)`"""
    if _func is None:
        return autocell(set_settings=set_settings, set_name=False)
    else:
        return autocell(_func)


def dict2name(prefix: Optional[str] = None, **kwargs: dict[str, Any]) -> str:
    """returns name from a dict"""
    label = [prefix] if prefix else []
    for key, value in kwargs.items():
        key = join_first_letters(key)
        label += [f"{key.upper()}{clean_value(value)}"]
    _label = "_".join(label)
    return clean_name(_label)


def get_component_name(component_type: str, **kwargs: dict[str, Any]) -> str:
    name = component_type

    if kwargs:
        name += f"_{dict2name(None, **kwargs)}"

    return name


def join_first_letters(name: str) -> str:
    """join the first letter of a name separated with underscores (taper_length -> TL)"""
    return "".join([x[0] for x in name.split("_") if x])


def clean_value(
    value: float | np.float64 | dict[Any, Any] | KCell | Callable[..., Any]
) -> str:
    """returns more readable value (integer)
    if number is < 1:
        returns number units in nm (integer)
    """

    try:
        if isinstance(value, int):  # integer
            return str(value)
        elif type(value) in [float, np.float64]:  # float
            return f"{value:.4f}".replace(".", "p").rstrip("0").rstrip("p")
        elif isinstance(value, list):
            return "_".join(clean_value(v) for v in value)
        elif isinstance(value, tuple):
            return "_".join(clean_value(v) for v in value)
        elif isinstance(value, dict):
            return dict2name(**value)
        elif isinstance(value, KCell):
            return clean_name(value.name)
        elif callable(value):
            return str(value.__name__)
        else:
            return clean_name(str(value))
    except TypeError:  # use the __str__ method
        return clean_name(str(value))


def clean_name(name: str) -> str:
    r"""Ensures that gds cells are composed of [a-zA-Z0-9_\-]

    FIXME: only a few characters are currently replaced.
        This function has been updated only on case-by-case basis
    """
    replace_map = {
        "=": "",
        ",": "_",
        ")": "",
        "(": "",
        "-": "m",
        ".": "p",
        ":": "_",
        "[": "",
        "]": "",
        " ": "_",
    }
    for k, v in list(replace_map.items()):
        name = name.replace(k, v)
    return name


DEFAULT_TRANS: dict[str, Union[str, int, float, dict[str, Union[str, int, float]]]] = {
    "x": "E",
    "y": "S",
    "x0": "W",
    "y0": "S",
    "margin": {
        "x": 10000,
        "y": 10000,
        "x0": 0,
        "y0": 0,
    },
    "ref": -2,
}


def update_default_trans(
    new_trans: dict[str, Union[str, int, float, dict[str, Union[str, int, float]]]]
) -> None:
    DEFAULT_TRANS.update(new_trans)


class KCellCache(Cache[int, Any]):
    def popitem(self) -> tuple[int, Any]:
        key, value = super().popitem()
        warnings.warn(
            f"KCell {value.name} was evicted from he cache. You probably should increase the cache size"
        )
        return key, value


__all__ = [
    "KCell",
    "Instance",
    "Port",
    "Ports",
    "autocell",
    "cell",
    "library",
    "KLib",
    "default_save",
    "ICplxPort",
    "DCplxPort",
    "DPort",
    "LayerEnum",
]
