"""A camera converts the mobjects contained in a Scene into an array of pixels."""

from __future__ import annotations

__all__ = ["Camera", "BackgroundColoredVMobjectDisplayer"]

import copy
import itertools as it
import operator as op
import pathlib
from collections.abc import Iterable
from functools import reduce
from typing import TYPE_CHECKING, Any, Callable

import cairo
import numpy as np
from PIL import Image
from scipy.spatial.distance import pdist
from typing_extensions import Self

from manim.typing import MatrixMN, PixelArray, Point3D, Point3D_Array

from .. import config, logger
from ..constants import *
from ..mobject.mobject import Mobject
from ..mobject.types.point_cloud_mobject import PMobject
from ..mobject.types.vectorized_mobject import VMobject
from ..utils.color import ManimColor, ParsableManimColor, color_to_int_rgba
from ..utils.family import extract_mobject_family_members
from ..utils.images import get_full_raster_image_path
from ..utils.iterables import list_difference_update
from ..utils.space_ops import angle_of_vector

if TYPE_CHECKING:
    from ..mobject.types.image_mobject import AbstractImageMobject


LINE_JOIN_MAP = {
    LineJointType.AUTO: None,  # TODO: this could be improved
    LineJointType.ROUND: cairo.LineJoin.ROUND,
    LineJointType.BEVEL: cairo.LineJoin.BEVEL,
    LineJointType.MITER: cairo.LineJoin.MITER,
}


CAP_STYLE_MAP = {
    CapStyleType.AUTO: None,  # TODO: this could be improved
    CapStyleType.ROUND: cairo.LineCap.ROUND,
    CapStyleType.BUTT: cairo.LineCap.BUTT,
    CapStyleType.SQUARE: cairo.LineCap.SQUARE,
}


class Camera:
    """Base camera class.

    This is the object which takes care of what exactly is displayed
    on screen at any given moment.

    Parameters
    ----------
    background_image
        The path to an image that should be the background image.
        If not set, the background is filled with :attr:`self.background_color`
    background
        What :attr:`background` is set to. By default, ``None``.
    pixel_height
        The height of the scene in pixels.
    pixel_width
        The width of the scene in pixels.
    kwargs
        Additional arguments (``background_color``, ``background_opacity``)
        to be set.
    """

    def __init__(
        self,
        background_image: str | None = None,
        frame_center: Point3D = ORIGIN,
        image_mode: str = "RGBA",
        n_channels: int = 4,
        pixel_array_dtype: str = "uint8",
        cairo_line_width_multiple: float = 0.01,
        use_z_index: bool = True,
        background: PixelArray | None = None,
        pixel_height: int | None = None,
        pixel_width: int | None = None,
        frame_height: float | None = None,
        frame_width: float | None = None,
        frame_rate: float | None = None,
        background_color: ParsableManimColor | None = None,
        background_opacity: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.background_image = background_image
        self.frame_center = frame_center
        self.image_mode = image_mode
        self.n_channels = n_channels
        self.pixel_array_dtype = pixel_array_dtype
        self.cairo_line_width_multiple = cairo_line_width_multiple
        self.use_z_index = use_z_index
        self.background = background
        self.background_colored_vmobject_displayer: (
            BackgroundColoredVMobjectDisplayer | None
        ) = None

        if pixel_height is None:
            pixel_height = config["pixel_height"]
        self.pixel_height = pixel_height

        if pixel_width is None:
            pixel_width = config["pixel_width"]
        self.pixel_width = pixel_width

        if frame_height is None:
            frame_height = config["frame_height"]
        self.frame_height = frame_height

        if frame_width is None:
            frame_width = config["frame_width"]
        self.frame_width = frame_width

        if frame_rate is None:
            frame_rate = config["frame_rate"]
        self.frame_rate = frame_rate

        if background_color is None:
            self._background_color: ManimColor = ManimColor.parse(
                config["background_color"]
            )
        else:
            self._background_color = ManimColor.parse(background_color)
        if background_opacity is None:
            self._background_opacity: float = config["background_opacity"]
        else:
            self._background_opacity = background_opacity

        # This one is in the same boat as the above, but it doesn't have the
        # same name as the corresponding key so it has to be handled on its own
        self.max_allowable_norm = config["frame_width"]

        self.rgb_max_val = np.iinfo(self.pixel_array_dtype).max
        self.pixel_array_to_cairo_context: dict[int, cairo.Context] = {}

        # Contains the correct method to process a list of Mobjects of the
        # corresponding class.  If a Mobject is not an instance of a class in
        # this dict (or an instance of a class that inherits from a class in
        # this dict), then it cannot be rendered.

        self.init_background()
        self.resize_frame_shape()
        self.reset()

    def __deepcopy__(self, memo: Any) -> Camera:
        # This is to address a strange bug where deepcopying
        # will result in a segfault, which is somehow related
        # to the aggdraw library
        self.canvas = None
        return copy.copy(self)

    @property
    def background_color(self) -> ManimColor:
        return self._background_color

    @background_color.setter
    def background_color(self, color: ManimColor) -> None:
        self._background_color = color
        self.init_background()

    @property
    def background_opacity(self) -> float:
        return self._background_opacity

    @background_opacity.setter
    def background_opacity(self, alpha: float) -> None:
        self._background_opacity = alpha
        self.init_background()

    def type_or_raise(
        self, mobject: Mobject
    ) -> type[VMobject] | type[PMobject] | type[AbstractImageMobject] | type[Mobject]:
        """Return the type of mobject, if it is a type that can be rendered.

        If `mobject` is an instance of a class that inherits from a class that
        can be rendered, return the super class.  For example, an instance of a
        Square is also an instance of VMobject, and these can be rendered.
        Therefore, `type_or_raise(Square())` returns True.

        Parameters
        ----------
        mobject
            The object to take the type of.

        Notes
        -----
        For a list of classes that can currently be rendered, see :meth:`display_funcs`.

        Returns
        -------
        Type[:class:`~.Mobject`]
            The type of mobjects, if it can be rendered.

        Raises
        ------
        :exc:`TypeError`
            When mobject is not an instance of a class that can be rendered.
        """
        from ..mobject.types.image_mobject import AbstractImageMobject

        self.display_funcs: dict[
            type[Mobject], Callable[[list[Mobject], PixelArray], Any]
        ] = {
            VMobject: self.display_multiple_vectorized_mobjects,  # type: ignore[dict-item]
            PMobject: self.display_multiple_point_cloud_mobjects,
            AbstractImageMobject: self.display_multiple_image_mobjects,
            Mobject: lambda batch, pa: batch,  # Do nothing
        }
        # We have to check each type in turn because we are dealing with
        # super classes.  For example, if square = Square(), then
        # type(square) != VMobject, but isinstance(square, VMobject) == True.
        for _type in self.display_funcs:
            if isinstance(mobject, _type):
                return _type
        raise TypeError(f"Displaying an object of class {_type} is not supported")

    def reset_pixel_shape(self, new_height: float, new_width: float) -> None:
        """This method resets the height and width
        of a single pixel to the passed new_height and new_width.

        Parameters
        ----------
        new_height
            The new height of the entire scene in pixels
        new_width
            The new width of the entire scene in pixels
        """
        self.pixel_width = new_width
        self.pixel_height = new_height
        self.init_background()
        self.resize_frame_shape()
        self.reset()

    def resize_frame_shape(self, fixed_dimension: int = 0) -> None:
        """
        Changes frame_shape to match the aspect ratio
        of the pixels, where fixed_dimension determines
        whether frame_height or frame_width
        remains fixed while the other changes accordingly.

        Parameters
        ----------
        fixed_dimension
            If 0, height is scaled with respect to width
            else, width is scaled with respect to height.
        """
        pixel_height = self.pixel_height
        pixel_width = self.pixel_width
        frame_height = self.frame_height
        frame_width = self.frame_width
        aspect_ratio = pixel_width / pixel_height
        if fixed_dimension == 0:
            frame_height = frame_width / aspect_ratio
        else:
            frame_width = aspect_ratio * frame_height
        self.frame_height = frame_height
        self.frame_width = frame_width

    def init_background(self) -> None:
        """Initialize the background.
        If self.background_image is the path of an image
        the image is set as background; else, the default
        background color fills the background.
        """
        height = self.pixel_height
        width = self.pixel_width
        if self.background_image is not None:
            path = get_full_raster_image_path(self.background_image)
            image = Image.open(path).convert(self.image_mode)
            # TODO, how to gracefully handle backgrounds
            # with different sizes?
            self.background = np.array(image)[:height, :width]
            self.background = self.background.astype(self.pixel_array_dtype)
        else:
            background_rgba = color_to_int_rgba(
                self.background_color,
                self.background_opacity,
            )
            self.background = np.zeros(
                (height, width, self.n_channels),
                dtype=self.pixel_array_dtype,
            )
            self.background[:, :] = background_rgba

    def get_image(
        self, pixel_array: PixelArray | list | tuple | None = None
    ) -> Image.Image:
        """Returns an image from the passed
        pixel array, or from the current frame
        if the passed pixel array is none.

        Parameters
        ----------
        pixel_array
            The pixel array from which to get an image, by default None

        Returns
        -------
        PIL.Image.Image
            The PIL image of the array.
        """
        if pixel_array is None:
            pixel_array = self.pixel_array
        return Image.fromarray(pixel_array, mode=self.image_mode)

    def convert_pixel_array(
        self, pixel_array: PixelArray | list | tuple, convert_from_floats: bool = False
    ) -> PixelArray:
        """Converts a pixel array from values that have floats in then
        to proper RGB values.

        Parameters
        ----------
        pixel_array
            Pixel array to convert.
        convert_from_floats
            Whether or not to convert float values to ints, by default False

        Returns
        -------
        np.array
            The new, converted pixel array.
        """
        retval = np.array(pixel_array)
        if convert_from_floats:
            retval = np.apply_along_axis(
                lambda f: (f * self.rgb_max_val).astype(self.pixel_array_dtype),
                2,
                retval,
            )
        return retval

    def set_pixel_array(
        self, pixel_array: PixelArray | list | tuple, convert_from_floats: bool = False
    ) -> None:
        """Sets the pixel array of the camera to the passed pixel array.

        Parameters
        ----------
        pixel_array
            The pixel array to convert and then set as the camera's pixel array.
        convert_from_floats
            Whether or not to convert float values to proper RGB values, by default False
        """
        converted_array: PixelArray = self.convert_pixel_array(
            pixel_array, convert_from_floats
        )
        if not (
            hasattr(self, "pixel_array")
            and self.pixel_array.shape == converted_array.shape
        ):
            self.pixel_array: PixelArray = converted_array
        else:
            # Set in place
            self.pixel_array[:, :, :] = converted_array[:, :, :]

    def set_background(
        self, pixel_array: PixelArray | list | tuple, convert_from_floats: bool = False
    ) -> None:
        """Sets the background to the passed pixel_array after converting
        to valid RGB values.

        Parameters
        ----------
        pixel_array
            The pixel array to set the background to.
        convert_from_floats
            Whether or not to convert floats values to proper RGB valid ones, by default False
        """
        self.background = self.convert_pixel_array(pixel_array, convert_from_floats)

    # TODO, this should live in utils, not as a method of Camera
    def make_background_from_func(
        self, coords_to_colors_func: Callable[[np.ndarray], np.ndarray]
    ) -> PixelArray:
        """
        Makes a pixel array for the background by using coords_to_colors_func to determine each pixel's color. Each input
        pixel's color. Each input to coords_to_colors_func is an (x, y) pair in space (in ordinary space coordinates; not
        pixel coordinates), and each output is expected to be an RGBA array of 4 floats.

        Parameters
        ----------
        coords_to_colors_func
            The function whose input is an (x,y) pair of coordinates and
            whose return values must be the colors for that point

        Returns
        -------
        np.array
            The pixel array which can then be passed to set_background.
        """
        logger.info("Starting set_background")
        coords = self.get_coords_of_all_pixels()
        new_background = np.apply_along_axis(coords_to_colors_func, 2, coords)
        logger.info("Ending set_background")

        return self.convert_pixel_array(new_background, convert_from_floats=True)

    def set_background_from_func(
        self, coords_to_colors_func: Callable[[np.ndarray], np.ndarray]
    ) -> None:
        """
        Sets the background to a pixel array using coords_to_colors_func to determine each pixel's color. Each input
        pixel's color. Each input to coords_to_colors_func is an (x, y) pair in space (in ordinary space coordinates; not
        pixel coordinates), and each output is expected to be an RGBA array of 4 floats.

        Parameters
        ----------
        coords_to_colors_func
            The function whose input is an (x,y) pair of coordinates and
            whose return values must be the colors for that point
        """
        self.set_background(self.make_background_from_func(coords_to_colors_func))

    def reset(self) -> Self:
        """Resets the camera's pixel array
        to that of the background

        Returns
        -------
        Camera
            The camera object after setting the pixel array.
        """
        self.set_pixel_array(self.background)
        return self

    def set_frame_to_background(self, background: PixelArray) -> None:
        self.set_pixel_array(background)

    ####

    def get_mobjects_to_display(
        self,
        mobjects: Iterable[Mobject],
        include_submobjects: bool = True,
        excluded_mobjects: list | None = None,
    ) -> list[Mobject]:
        """Used to get the list of mobjects to display
        with the camera.

        Parameters
        ----------
        mobjects
            The Mobjects
        include_submobjects
            Whether or not to include the submobjects of mobjects, by default True
        excluded_mobjects
            Any mobjects to exclude, by default None

        Returns
        -------
        list
            list of mobjects
        """
        if include_submobjects:
            mobjects = extract_mobject_family_members(
                mobjects,
                use_z_index=self.use_z_index,
                only_those_with_points=True,
            )
            if excluded_mobjects:
                all_excluded = extract_mobject_family_members(
                    excluded_mobjects,
                    use_z_index=self.use_z_index,
                )
                mobjects = list_difference_update(mobjects, all_excluded)
        return list(mobjects)

    def is_in_frame(self, mobject: Mobject) -> bool:
        """Checks whether the passed mobject is in
        frame or not.

        Parameters
        ----------
        mobject
            The mobject for which the checking needs to be done.

        Returns
        -------
        bool
            True if in frame, False otherwise.
        """
        fc = self.frame_center
        fh = self.frame_height
        fw = self.frame_width
        return not reduce(
            op.or_,
            [
                mobject.get_right()[0] < fc[0] - fw / 2,
                mobject.get_bottom()[1] > fc[1] + fh / 2,
                mobject.get_left()[0] > fc[0] + fw / 2,
                mobject.get_top()[1] < fc[1] - fh / 2,
            ],
        )

    def capture_mobject(self, mobject: Mobject, **kwargs: Any) -> None:
        """Capture mobjects by storing it in :attr:`pixel_array`.

        This is a single-mobject version of :meth:`capture_mobjects`.

        Parameters
        ----------
        mobject
            Mobject to capture.

        kwargs
            Keyword arguments to be passed to :meth:`get_mobjects_to_display`.

        """
        return self.capture_mobjects([mobject], **kwargs)

    def capture_mobjects(self, mobjects: Iterable[Mobject], **kwargs: Any) -> None:
        """Capture mobjects by printing them on :attr:`pixel_array`.

        This is the essential function that converts the contents of a Scene
        into an array, which is then converted to an image or video.

        Parameters
        ----------
        mobjects
            Mobjects to capture.

        kwargs
            Keyword arguments to be passed to :meth:`get_mobjects_to_display`.

        Notes
        -----
        For a list of classes that can currently be rendered, see :meth:`display_funcs`.

        """
        # The mobjects will be processed in batches (or runs) of mobjects of
        # the same type.  That is, if the list mobjects contains objects of
        # types [VMobject, VMobject, VMobject, PMobject, PMobject, VMobject],
        # then they will be captured in three batches: [VMobject, VMobject,
        # VMobject], [PMobject, PMobject], and [VMobject].  This must be done
        # without altering their order.  it.groupby computes exactly this
        # partition while at the same time preserving order.
        mobjects = self.get_mobjects_to_display(mobjects, **kwargs)
        for group_type, group in it.groupby(mobjects, self.type_or_raise):
            self.display_funcs[group_type](list(group), self.pixel_array)

    # Methods associated with svg rendering

    # NOTE: None of the methods below have been mentioned outside of their definitions. Their DocStrings are not as
    # detailed as possible.

    def get_cached_cairo_context(self, pixel_array: PixelArray) -> cairo.Context | None:
        """Returns the cached cairo context of the passed
        pixel array if it exists, and None if it doesn't.

        Parameters
        ----------
        pixel_array
            The pixel array to check.

        Returns
        -------
        cairo.Context
            The cached cairo context.
        """
        return self.pixel_array_to_cairo_context.get(id(pixel_array), None)

    def cache_cairo_context(self, pixel_array: PixelArray, ctx: cairo.Context) -> None:
        """Caches the passed Pixel array into a Cairo Context

        Parameters
        ----------
        pixel_array
            The pixel array to cache
        ctx
            The context to cache it into.
        """
        self.pixel_array_to_cairo_context[id(pixel_array)] = ctx

    def get_cairo_context(self, pixel_array: PixelArray) -> cairo.Context:
        """Returns the cairo context for a pixel array after
        caching it to self.pixel_array_to_cairo_context
        If that array has already been cached, it returns the
        cached version instead.

        Parameters
        ----------
        pixel_array
            The Pixel array to get the cairo context of.

        Returns
        -------
        cairo.Context
            The cairo context of the pixel array.
        """
        cached_ctx = self.get_cached_cairo_context(pixel_array)
        if cached_ctx:
            return cached_ctx
        pw = self.pixel_width
        ph = self.pixel_height
        fw = self.frame_width
        fh = self.frame_height
        fc = self.frame_center
        surface = cairo.ImageSurface.create_for_data(
            pixel_array.data,
            cairo.FORMAT_ARGB32,
            pw,
            ph,
        )
        ctx = cairo.Context(surface)
        ctx.scale(pw, ph)
        ctx.set_matrix(
            cairo.Matrix(
                (pw / fw),
                0,
                0,
                -(ph / fh),
                (pw / 2) - fc[0] * (pw / fw),
                (ph / 2) + fc[1] * (ph / fh),
            ),
        )
        self.cache_cairo_context(pixel_array, ctx)
        return ctx

    def display_multiple_vectorized_mobjects(
        self, vmobjects: list[VMobject], pixel_array: PixelArray
    ) -> None:
        """Displays multiple VMobjects in the pixel_array

        Parameters
        ----------
        vmobjects
            list of VMobjects to display
        pixel_array
            The pixel array
        """
        if len(vmobjects) == 0:
            return
        batch_image_pairs = it.groupby(vmobjects, lambda vm: vm.get_background_image())
        for image, batch in batch_image_pairs:
            if image:
                self.display_multiple_background_colored_vmobjects(batch, pixel_array)
            else:
                self.display_multiple_non_background_colored_vmobjects(
                    batch,
                    pixel_array,
                )

    def display_multiple_non_background_colored_vmobjects(
        self, vmobjects: Iterable[VMobject], pixel_array: PixelArray
    ) -> None:
        """Displays multiple VMobjects in the cairo context, as long as they don't have
        background colors.

        Parameters
        ----------
        vmobjects
            list of the VMobjects
        pixel_array
            The Pixel array to add the VMobjects to.
        """
        ctx = self.get_cairo_context(pixel_array)
        for vmobject in vmobjects:
            self.display_vectorized(vmobject, ctx)

    def display_vectorized(self, vmobject: VMobject, ctx: cairo.Context) -> Self:
        """Displays a VMobject in the cairo context

        Parameters
        ----------
        vmobject
            The Vectorized Mobject to display
        ctx
            The cairo context to use.

        Returns
        -------
        Camera
            The camera object
        """
        self.set_cairo_context_path(ctx, vmobject)
        self.apply_stroke(ctx, vmobject, background=True)
        self.apply_fill(ctx, vmobject)
        self.apply_stroke(ctx, vmobject)
        return self

    def set_cairo_context_path(self, ctx: cairo.Context, vmobject: VMobject) -> Self:
        """Sets a path for the cairo context with the vmobject passed

        Parameters
        ----------
        ctx
            The cairo context
        vmobject
            The VMobject

        Returns
        -------
        Camera
            Camera object after setting cairo_context_path
        """
        points = self.transform_points_pre_display(vmobject, vmobject.points)
        # TODO, shouldn't this be handled in transform_points_pre_display?
        # points = points - self.get_frame_center()
        if len(points) == 0:
            return self

        ctx.new_path()
        subpaths = vmobject.gen_subpaths_from_points_2d(points)
        for subpath in subpaths:
            quads = vmobject.gen_cubic_bezier_tuples_from_points(subpath)
            ctx.new_sub_path()
            start = subpath[0]
            ctx.move_to(*start[:2])
            for _p0, p1, p2, p3 in quads:
                ctx.curve_to(*p1[:2], *p2[:2], *p3[:2])
            if vmobject.consider_points_equals_2d(subpath[0], subpath[-1]):
                ctx.close_path()
        return self

    def set_cairo_context_color(
        self, ctx: cairo.Context, rgbas: MatrixMN, vmobject: VMobject
    ) -> Self:
        """Sets the color of the cairo context

        Parameters
        ----------
        ctx
            The cairo context
        rgbas
            The RGBA array with which to color the context.
        vmobject
            The VMobject with which to set the color.

        Returns
        -------
        Camera
            The camera object
        """
        if len(rgbas) == 1:
            # Use reversed rgb because cairo surface is
            # encodes it in reverse order
            ctx.set_source_rgba(*rgbas[0][2::-1], rgbas[0][3])
        else:
            points = vmobject.get_gradient_start_and_end_points()
            points = self.transform_points_pre_display(vmobject, points)
            pat = cairo.LinearGradient(*it.chain(*(point[:2] for point in points)))
            step = 1.0 / (len(rgbas) - 1)
            offsets = np.arange(0, 1 + step, step)
            for rgba, offset in zip(rgbas, offsets):
                pat.add_color_stop_rgba(offset, *rgba[2::-1], rgba[3])
            ctx.set_source(pat)
        return self

    def apply_fill(self, ctx: cairo.Context, vmobject: VMobject) -> Self:
        """Fills the cairo context

        Parameters
        ----------
        ctx
            The cairo context
        vmobject
            The VMobject

        Returns
        -------
        Camera
            The camera object.
        """
        self.set_cairo_context_color(ctx, self.get_fill_rgbas(vmobject), vmobject)
        ctx.fill_preserve()
        return self

    def apply_stroke(
        self, ctx: cairo.Context, vmobject: VMobject, background: bool = False
    ) -> Self:
        """Applies a stroke to the VMobject in the cairo context.

        Parameters
        ----------
        ctx
            The cairo context
        vmobject
            The VMobject
        background
            Whether or not to consider the background when applying this
            stroke width, by default False

        Returns
        -------
        Camera
            The camera object with the stroke applied.
        """
        width = vmobject.get_stroke_width(background)
        if width == 0:
            return self
        self.set_cairo_context_color(
            ctx,
            self.get_stroke_rgbas(vmobject, background=background),
            vmobject,
        )
        ctx.set_line_width(
            width
            * self.cairo_line_width_multiple
            * (self.frame_width / self.frame_width),
            # This ensures lines have constant width as you zoom in on them.
        )
        if vmobject.joint_type != LineJointType.AUTO:
            ctx.set_line_join(LINE_JOIN_MAP[vmobject.joint_type])
        if vmobject.cap_style != CapStyleType.AUTO:
            ctx.set_line_cap(CAP_STYLE_MAP[vmobject.cap_style])
        ctx.stroke_preserve()
        return self

    def get_stroke_rgbas(
        self, vmobject: VMobject, background: bool = False
    ) -> PixelArray:
        """Gets the RGBA array for the stroke of the passed
        VMobject.

        Parameters
        ----------
        vmobject
            The VMobject
        background
            Whether or not to consider the background when getting the stroke
            RGBAs, by default False

        Returns
        -------
        np.ndarray
            The RGBA array of the stroke.
        """
        return vmobject.get_stroke_rgbas(background)

    def get_fill_rgbas(self, vmobject: VMobject) -> PixelArray:
        """Returns the RGBA array of the fill of the passed VMobject

        Parameters
        ----------
        vmobject
            The VMobject

        Returns
        -------
        np.array
            The RGBA Array of the fill of the VMobject
        """
        return vmobject.get_fill_rgbas()

    def get_background_colored_vmobject_displayer(
        self,
    ) -> BackgroundColoredVMobjectDisplayer:
        """Returns the background_colored_vmobject_displayer
        if it exists or makes one and returns it if not.

        Returns
        -------
        BackgroundColoredVMobjectDisplayer
            Object that displays VMobjects that have the same color
            as the background.
        """
        if self.background_colored_vmobject_displayer is None:
            self.background_colored_vmobject_displayer = (
                BackgroundColoredVMobjectDisplayer(self)
            )
        return self.background_colored_vmobject_displayer

    def display_multiple_background_colored_vmobjects(
        self, cvmobjects: Iterable[VMobject], pixel_array: PixelArray
    ) -> Self:
        """Displays multiple vmobjects that have the same color as the background.

        Parameters
        ----------
        cvmobjects
            List of Colored VMobjects
        pixel_array
            The pixel array.

        Returns
        -------
        Camera
            The camera object.
        """
        displayer = self.get_background_colored_vmobject_displayer()
        cvmobject_pixel_array = displayer.display(*cvmobjects)
        self.overlay_rgba_array(pixel_array, cvmobject_pixel_array)
        return self

    # Methods for other rendering

    # NOTE: Out of the following methods, only `transform_points_pre_display` and `points_to_pixel_coords` have been mentioned outside of their definitions.
    # As a result, the other methods do not have as detailed docstrings as would be preferred.

    def display_multiple_point_cloud_mobjects(
        self, pmobjects: list, pixel_array: PixelArray
    ) -> None:
        """Displays multiple PMobjects by modifying the passed pixel array.

        Parameters
        ----------
        pmobjects
            List of PMobjects
        pixel_array
            The pixel array to modify.
        """
        for pmobject in pmobjects:
            self.display_point_cloud(
                pmobject,
                pmobject.points,
                pmobject.rgbas,
                self.adjusted_thickness(pmobject.stroke_width),
                pixel_array,
            )

    def display_point_cloud(
        self,
        pmobject: PMobject,
        points: list,
        rgbas: np.ndarray,
        thickness: float,
        pixel_array: PixelArray,
    ) -> None:
        """Displays a PMobject by modifying the pixel array suitably.

        TODO: Write a description for the rgbas argument.

        Parameters
        ----------
        pmobject
            Point Cloud Mobject
        points
            The points to display in the point cloud mobject
        rgbas

        thickness
            The thickness of each point of the PMobject
        pixel_array
            The pixel array to modify.

        """
        if len(points) == 0:
            return
        pixel_coords = self.points_to_pixel_coords(pmobject, points)
        pixel_coords = self.thickened_coordinates(pixel_coords, thickness)
        rgba_len = pixel_array.shape[2]

        rgbas = (self.rgb_max_val * rgbas).astype(self.pixel_array_dtype)
        target_len = len(pixel_coords)
        factor = target_len // len(rgbas)
        rgbas = np.array([rgbas] * factor).reshape((target_len, rgba_len))

        on_screen_indices = self.on_screen_pixels(pixel_coords)
        pixel_coords = pixel_coords[on_screen_indices]
        rgbas = rgbas[on_screen_indices]

        ph = self.pixel_height
        pw = self.pixel_width

        flattener = np.array([1, pw], dtype="int")
        flattener = flattener.reshape((2, 1))
        indices = np.dot(pixel_coords, flattener)[:, 0]
        indices = indices.astype("int")

        new_pa = pixel_array.reshape((ph * pw, rgba_len))
        new_pa[indices] = rgbas
        pixel_array[:, :] = new_pa.reshape((ph, pw, rgba_len))

    def display_multiple_image_mobjects(
        self, image_mobjects: list, pixel_array: np.ndarray
    ) -> None:
        """Displays multiple image mobjects by modifying the passed pixel_array.

        Parameters
        ----------
        image_mobjects
            list of ImageMobjects
        pixel_array
            The pixel array to modify.
        """
        for image_mobject in image_mobjects:
            self.display_image_mobject(image_mobject, pixel_array)

    def display_image_mobject(
        self, image_mobject: AbstractImageMobject, pixel_array: np.ndarray
    ) -> None:
        """Displays an ImageMobject by changing the pixel_array suitably.

        Parameters
        ----------
        image_mobject
            The imageMobject to display
        pixel_array
            The Pixel array to put the imagemobject in.
        """
        corner_coords = self.points_to_pixel_coords(image_mobject, image_mobject.points)
        ul_coords, ur_coords, dl_coords, _ = corner_coords
        right_vect = ur_coords - ul_coords
        down_vect = dl_coords - ul_coords
        center_coords = ul_coords + (right_vect + down_vect) / 2

        sub_image = Image.fromarray(image_mobject.get_pixel_array(), mode="RGBA")

        # Reshape
        pixel_width = max(int(pdist([ul_coords, ur_coords]).item()), 1)
        pixel_height = max(int(pdist([ul_coords, dl_coords]).item()), 1)
        sub_image = sub_image.resize(
            (pixel_width, pixel_height),
            resample=image_mobject.resampling_algorithm,
        )

        # Rotate
        angle = angle_of_vector(right_vect)
        adjusted_angle = -int(360 * angle / TAU)
        if adjusted_angle != 0:
            sub_image = sub_image.rotate(
                adjusted_angle,
                resample=image_mobject.resampling_algorithm,
                expand=1,
            )

        # TODO, there is no accounting for a shear...

        # Paste into an image as large as the camera's pixel array
        full_image = Image.fromarray(
            np.zeros((self.pixel_height, self.pixel_width)),
            mode="RGBA",
        )
        new_ul_coords = center_coords - np.array(sub_image.size) / 2
        new_ul_coords = new_ul_coords.astype(int)
        full_image.paste(
            sub_image,
            box=(
                new_ul_coords[0],
                new_ul_coords[1],
                new_ul_coords[0] + sub_image.size[0],
                new_ul_coords[1] + sub_image.size[1],
            ),
        )
        # Paint on top of existing pixel array
        self.overlay_PIL_image(pixel_array, full_image)

    def overlay_rgba_array(
        self, pixel_array: np.ndarray, new_array: np.ndarray
    ) -> None:
        """Overlays an RGBA array on top of the given Pixel array.

        Parameters
        ----------
        pixel_array
            The original pixel array to modify.
        new_array
            The new pixel array to overlay.
        """
        self.overlay_PIL_image(pixel_array, self.get_image(new_array))

    def overlay_PIL_image(self, pixel_array: np.ndarray, image: Image) -> None:
        """Overlays a PIL image on the passed pixel array.

        Parameters
        ----------
        pixel_array
            The Pixel array
        image
            The Image to overlay.
        """
        pixel_array[:, :] = np.array(
            Image.alpha_composite(self.get_image(pixel_array), image),
            dtype="uint8",
        )

    def adjust_out_of_range_points(self, points: np.ndarray) -> np.ndarray:
        """If any of the points in the passed array are out of
        the viable range, they are adjusted suitably.

        Parameters
        ----------
        points
            The points to adjust

        Returns
        -------
        np.array
            The adjusted points.
        """
        if not np.any(points > self.max_allowable_norm):
            return points
        norms = np.apply_along_axis(np.linalg.norm, 1, points)
        violator_indices = norms > self.max_allowable_norm
        violators = points[violator_indices, :]
        violator_norms = norms[violator_indices]
        reshaped_norms = np.repeat(
            violator_norms.reshape((len(violator_norms), 1)),
            points.shape[1],
            1,
        )
        rescaled = self.max_allowable_norm * violators / reshaped_norms
        points[violator_indices] = rescaled
        return points

    def transform_points_pre_display(
        self,
        mobject: Mobject,
        points: Point3D_Array,
    ) -> Point3D_Array:  # TODO: Write more detailed docstrings for this method.
        # NOTE: There seems to be an unused argument `mobject`.

        # Subclasses (like ThreeDCamera) may want to
        # adjust points further before they're shown
        if not np.all(np.isfinite(points)):
            # TODO, print some kind of warning about
            # mobject having invalid points?
            points = np.zeros((1, 3))
        return points

    def points_to_pixel_coords(
        self,
        mobject: Mobject,
        points: np.ndarray,
    ) -> np.ndarray:  # TODO: Write more detailed docstrings for this method.
        points = self.transform_points_pre_display(mobject, points)
        shifted_points = points - self.frame_center

        result = np.zeros((len(points), 2))
        pixel_height = self.pixel_height
        pixel_width = self.pixel_width
        frame_height = self.frame_height
        frame_width = self.frame_width
        width_mult = pixel_width / frame_width
        width_add = pixel_width / 2
        height_mult = pixel_height / frame_height
        height_add = pixel_height / 2
        # Flip on y-axis as you go
        height_mult *= -1

        result[:, 0] = shifted_points[:, 0] * width_mult + width_add
        result[:, 1] = shifted_points[:, 1] * height_mult + height_add
        return result.astype("int")

    def on_screen_pixels(self, pixel_coords: np.ndarray) -> PixelArray:
        """Returns array of pixels that are on the screen from a given
        array of pixel_coordinates

        Parameters
        ----------
        pixel_coords
            The pixel coords to check.

        Returns
        -------
        np.array
            The pixel coords on screen.
        """
        return reduce(
            op.and_,
            [
                pixel_coords[:, 0] >= 0,
                pixel_coords[:, 0] < self.pixel_width,
                pixel_coords[:, 1] >= 0,
                pixel_coords[:, 1] < self.pixel_height,
            ],
        )

    def adjusted_thickness(self, thickness: float) -> float:
        """Computes the adjusted stroke width for a zoomed camera.

        Parameters
        ----------
        thickness
            The stroke width of a mobject.

        Returns
        -------
        float
            The adjusted stroke width that reflects zooming in with
            the camera.
        """
        # TODO: This seems...unsystematic
        big_sum: float = op.add(config["pixel_height"], config["pixel_width"])
        this_sum: float = op.add(self.pixel_height, self.pixel_width)
        factor = big_sum / this_sum
        return 1 + (thickness - 1) * factor

    def get_thickening_nudges(self, thickness: float) -> PixelArray:
        """Determine a list of vectors used to nudge
        two-dimensional pixel coordinates.

        Parameters
        ----------
        thickness

        Returns
        -------
        np.array

        """
        thickness = int(thickness)
        _range = list(range(-thickness // 2 + 1, thickness // 2 + 1))
        return np.array(list(it.product(_range, _range)))

    def thickened_coordinates(
        self, pixel_coords: np.ndarray, thickness: float
    ) -> PixelArray:
        """Returns thickened coordinates for a passed array of pixel coords and
        a thickness to thicken by.

        Parameters
        ----------
        pixel_coords
            Pixel coordinates
        thickness
            Thickness

        Returns
        -------
        np.array
            Array of thickened pixel coords.
        """
        nudges = self.get_thickening_nudges(thickness)
        pixel_coords = np.array([pixel_coords + nudge for nudge in nudges])
        size = pixel_coords.size
        return pixel_coords.reshape((size // 2, 2))

    # TODO, reimplement using cairo matrix
    def get_coords_of_all_pixels(self) -> PixelArray:
        """Returns the cartesian coordinates of each pixel.

        Returns
        -------
        np.ndarray
            The array of cartesian coordinates.
        """
        # These are in x, y order, to help me keep things straight
        full_space_dims = np.array([self.frame_width, self.frame_height])
        full_pixel_dims = np.array([self.pixel_width, self.pixel_height])

        # These are addressed in the same y, x order as in pixel_array, but the values in them
        # are listed in x, y order
        uncentered_pixel_coords = np.indices([self.pixel_height, self.pixel_width])[
            ::-1
        ].transpose(1, 2, 0)
        uncentered_space_coords = (
            uncentered_pixel_coords * full_space_dims
        ) / full_pixel_dims
        # Could structure above line's computation slightly differently, but figured (without much
        # thought) multiplying by frame_shape first, THEN dividing by pixel_shape, is probably
        # better than the other order, for avoiding underflow quantization in the division (whereas
        # overflow is unlikely to be a problem)

        centered_space_coords = uncentered_space_coords - (full_space_dims / 2)

        # Have to also flip the y coordinates to account for pixel array being listed in
        # top-to-bottom order, opposite of screen coordinate convention
        centered_space_coords = centered_space_coords * (1, -1)

        return centered_space_coords


# NOTE: The methods of the following class have not been mentioned outside of their definitions.
# Their DocStrings are not as detailed as preferred.
class BackgroundColoredVMobjectDisplayer:
    """Auxiliary class that handles displaying vectorized mobjects with
    a set background image.

    Parameters
    ----------
    camera
        Camera object to use.
    """

    def __init__(self, camera: Camera):
        self.camera = camera
        self.file_name_to_pixel_array_map: dict[str, PixelArray] = {}
        self.pixel_array = np.array(camera.pixel_array)
        self.reset_pixel_array()

    def reset_pixel_array(self) -> None:
        self.pixel_array[:, :] = 0

    def resize_background_array(
        self,
        background_array: PixelArray,
        new_width: float,
        new_height: float,
        mode: str = "RGBA",
    ) -> PixelArray:
        """Resizes the pixel array representing the background.

        Parameters
        ----------
        background_array
            The pixel
        new_width
            The new width of the background
        new_height
            The new height of the background
        mode
            The PIL image mode, by default "RGBA"

        Returns
        -------
        np.array
            The numpy pixel array of the resized background.
        """
        image = Image.fromarray(background_array)
        image = image.convert(mode)
        resized_image = image.resize((new_width, new_height))
        return np.array(resized_image)

    def resize_background_array_to_match(
        self, background_array: PixelArray, pixel_array: PixelArray
    ) -> PixelArray:
        """Resizes the background array to match the passed pixel array.

        Parameters
        ----------
        background_array
            The prospective pixel array.
        pixel_array
            The pixel array whose width and height should be matched.

        Returns
        -------
        np.array
            The resized background array.
        """
        height, width = pixel_array.shape[:2]
        mode = "RGBA" if pixel_array.shape[2] == 4 else "RGB"
        return self.resize_background_array(background_array, width, height, mode)

    def get_background_array(
        self, image: Image.Image | pathlib.Path | str
    ) -> PixelArray:
        """Gets the background array that has the passed file_name.

        Parameters
        ----------
        image
            The background image or its file name.

        Returns
        -------
        np.ndarray
            The pixel array of the image.
        """
        image_key = str(image)

        if image_key in self.file_name_to_pixel_array_map:
            return self.file_name_to_pixel_array_map[image_key]
        if isinstance(image, str):
            full_path = get_full_raster_image_path(image)
            image = Image.open(full_path)
        back_array = np.array(image)

        pixel_array = self.pixel_array
        if not np.all(pixel_array.shape == back_array.shape):
            back_array = self.resize_background_array_to_match(back_array, pixel_array)

        self.file_name_to_pixel_array_map[image_key] = back_array
        return back_array

    def display(self, *cvmobjects: VMobject) -> PixelArray | None:
        """Displays the colored VMobjects.

        Parameters
        ----------
        *cvmobjects
            The VMobjects

        Returns
        -------
        np.array
            The pixel array with the `cvmobjects` displayed.
        """
        batch_image_pairs = it.groupby(cvmobjects, lambda cv: cv.get_background_image())
        curr_array = None
        for image, batch in batch_image_pairs:
            background_array = self.get_background_array(image)
            pixel_array = self.pixel_array
            self.camera.display_multiple_non_background_colored_vmobjects(
                batch,
                pixel_array,
            )
            new_array = np.array(
                (background_array * pixel_array.astype("float") / 255),
                dtype=self.camera.pixel_array_dtype,
            )
            if curr_array is None:
                curr_array = new_array
            else:
                curr_array = np.maximum(curr_array, new_array)
            self.reset_pixel_array()
        return curr_array
