from typing import Iterable, Optional

from ... import KCell, KLib, LayerEnum, kdb
from ...config import logger


class FillOperator(kdb.TileOutputReceiver):
    def __init__(
        self,
        klib: KLib,
        top_cell: KCell,
        fill_cell_index: int,
        fc_bbox: kdb.Box,
        row_step: kdb.Vector,
        column_step: kdb.Vector,
        fill_margin: kdb.Vector = kdb.Vector(0, 0),
        remaining_polygons: Optional[kdb.Region] = None,
        glue_box: kdb.Box = kdb.Box(),
    ) -> None:
        self.klib = klib
        self.top_cell = top_cell
        self.fill_cell_index = fill_cell_index
        self.fc_bbox = fc_bbox
        self.row_step = row_step
        self.column_step = column_step
        self.fill_margin = fill_margin
        self.remaining_polygons = remaining_polygons
        self.glue_box = glue_box

    def put(
        self,
        ix: int,
        iy: int,
        tile: kdb.Box,
        region: kdb.Region,
        dbu: float,
        clip: bool,
    ) -> None:
        self.top_cell.fill_region_multi(
            region,
            self.fill_cell_index,
            self.fc_bbox,
            self.row_step,
            self.column_step,
            self.fill_margin,
            self.remaining_polygons,
            self.glue_box,
        )


def fill_tiled(
    c: KCell,
    fill_cell: KCell,
    fill_layers: Iterable[tuple[LayerEnum | int, int]] = [],
    fill_regions: Iterable[tuple[kdb.Region, int]] = [],
    exclude_layers: Iterable[tuple[LayerEnum | int, int]] = [],
    exclude_regions: Iterable[tuple[kdb.Region, int]] = [],
    n_threads: int = 4,
    tile_size: Optional[tuple[float, float]] = None,
    x_space: float = 0,
    y_space: float = 0,
) -> None:
    tp = kdb.TilingProcessor()
    tp.frame = c.bbox().to_dtype(c.klib.dbu)  # type: ignore
    tp.dbu = c.klib.dbu
    tp.threads = n_threads

    if tile_size is None:
        tp.tile_size(
            100 * (fill_cell.dbbox().width() + x_space) + x_space,
            100 * (fill_cell.dbbox().height() + y_space) + y_space,
        )
    else:
        tp.tile_size(*tile_size)  # tile size in um
    # tp.tile_border(fill_cell.dbbox().width() * 2, fill_cell.dbbox().height() * 2)

    layer_names: list[str] = []
    for l, _ in fill_layers:
        layer_name = f"layer{l}"
        tp.input(layer_name, c.klib, c.cell_index(), c.klib.get_info(l))
        layer_names.append(layer_name)

    region_names: list[str] = []
    for i, (r, _) in enumerate(fill_regions):
        region_name = f"region{i}"
        tp.input(region_name, r)
        region_names.append(region_name)

    exlayer_names: list[str] = []
    for l, _ in exclude_layers:
        layer_name = f"layer{l}"
        tp.input(layer_name, c.klib, c.cell_index(), c.klib.get_info(l))
        exlayer_names.append(layer_name)

    exregion_names: list[str] = []
    for i, (r, _) in enumerate(exclude_regions):
        region_name = f"region{i}"
        tp.input(region_name, r)
        exregion_names.append(region_name)

    tp.output(
        "to_fill",
        FillOperator(
            c.klib,
            c,
            fill_cell.cell_index(),
            fc_bbox=fill_cell.bbox().enlarged(
                int(x_space / 2 / c.klib.dbu), int(y_space / 2 / c.klib.dbu)
            ),
            row_step=kdb.Vector(
                fill_cell.bbox().width() + int(x_space / 2 / c.klib.dbu), 0
            ),
            column_step=kdb.Vector(
                0, fill_cell.bbox().height() + int(y_space / 2 / c.klib.dbu)
            ),
        ),
    )

    if layer_names or region_names:
        print(fill_layers, fill_regions, exclude_regions, exclude_layers)

        for layer_name, (_, size) in zip(exlayer_names, exclude_layers):
            print(layer_name, size)
        exlayers = " + ".join(
            [
                layer_name + f".sized({int(size / c.klib.dbu)})" if size else layer_name
                for layer_name, (_, size) in zip(exlayer_names, exclude_layers)
            ]
        )
        exregions = " + ".join(
            [
                region_name + f".sized({int(size / c.klib.dbu)})"
                if size
                else region_name
                for region_name, (_, size) in zip(exregion_names, exclude_regions)
            ]
        )
        layers = " + ".join(
            [
                layer_name + f".sized({int(size / c.klib.dbu)})" if size else layer_name
                for layer_name, (_, size) in zip(layer_names, fill_layers)
            ]
        )
        regions = " + ".join(
            [
                region_name + f".sized({int(size / c.klib.dbu)})"
                if size
                else region_name
                for region_name, (_, size) in zip(region_names, fill_regions)
            ]
        )

        print(layers, regions, exlayers, exregions)
        if exlayer_names or exregion_names:
            queue_str = (
                "var fill= "
                + (
                    " + ".join([layers, regions])
                    if regions and layers
                    else regions + layers
                )
                + "; var exclude = "
                + (
                    " + ".join([exlayers, exregions])
                    if exregions and exlayers
                    else exregions + exlayers
                )
                + "; var fill_region = _tile & _frame & fill - exclude; _output(to_fill, fill_region)"
            )
        else:
            queue_str = (
                "var fill= "
                + (
                    " + ".join([layers, regions])
                    if regions and layers
                    else regions + layers
                )
                + "; var fill_region = _tile & _frame & fill; _output(to_fill, fill_region)"
            )
        tp.queue(queue_str)
        c.klib.start_changes()
        try:
            logger.info("filling {} with {}", c.name, fill_cell.name)
            logger.debug("layers: {}", fill_layers)
            logger.debug("fill string: '{}'", queue_str)
            tp.execute(f"Fill {c.name}")
            logger.info("done with filling {}", c.name)
        finally:
            c.klib.end_changes()
