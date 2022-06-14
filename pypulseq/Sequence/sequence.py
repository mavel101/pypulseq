import itertools
import math
from collections import OrderedDict
from types import SimpleNamespace
from typing import Tuple, List
from typing import Union
from warnings import warn

import matplotlib as mpl
import numpy as np
from matplotlib import pyplot as plt
from scipy import interpolate

from pypulseq import eps, major, minor, revision
from pypulseq.Sequence import block, parula
from pypulseq.Sequence.ext_test_report import ext_test_report
from pypulseq.Sequence.read_seq import read
from pypulseq.Sequence.write_seq import write as write_seq
from pypulseq.calc_rf_center import calc_rf_center
from pypulseq.check_timing import check_timing as ext_check_timing
from pypulseq.decompress_shape import decompress_shape
from pypulseq.event_lib import EventLibrary
from pypulseq.opts import Opts
from pypulseq.points_to_waveform import points_to_waveform
from pypulseq.supported_labels_rf_use import get_supported_labels


class Sequence:
    """
    Generate sequences and read/write sequence files. This class defines properties and methods to define a complete MR
    sequence including RF pulses, gradients, ADC events, etc. The class provides an implementation of the open MR
    sequence format defined by the Pulseq project. See http://pulseq.github.io/.

    See also `demo_read.py`, `demo_write.py`.
    """

    version_major: int = major
    version_minor: int = minor
    version_revision: int = revision

    def __init__(self, system=Opts()):
        # =========
        # EVENT LIBRARIES
        # =========
        self.adc_library = EventLibrary()  # Library of ADC events
        self.delay_library = EventLibrary()  # Library of delay events
        # Library of extension events. Extension events form single-linked zero-terminated lists
        self.extensions_library = EventLibrary()
        self.grad_library = EventLibrary()  # Library of gradient events
        self.label_inc_library = (
            EventLibrary()
        )  # Library of Label(inc) events (reference from the extensions library)
        self.label_set_library = (
            EventLibrary()
        )  # Library of Label(set) events (reference from the extensions library)
        self.rf_library = EventLibrary()  # Library of RF events
        self.shape_library = EventLibrary()  # Library of compressed shapes
        self.trigger_library = EventLibrary()  # Library of trigger events

        # =========
        # OTHER
        # =========
        self.system = system

        self.block_events = OrderedDict()  # Event table
        self.definitions = dict()  # Optional sequence definitions

        self.rf_raster_time = (
            self.system.rf_raster_time
        )  # RF raster time (system dependent)
        self.grad_raster_time = (
            self.system.grad_raster_time
        )  # Gradient raster time (system dependent)
        self.adc_raster_time = (
            self.system.adc_raster_time
        )  # ADC raster time (system dependent)
        self.block_duration_raster = self.system.block_duration_raster
        self.set_definition("AdcRasterTime", self.adc_raster_time)
        self.set_definition("BlockDurationRaster", self.block_duration_raster)
        self.set_definition("GradientRasterTime", self.grad_raster_time)
        self.set_definition("RadiofrequencyRasterTime", self.rf_raster_time)
        self.signature_type = ""
        self.signature_file = ""
        self.signature_value = ""

        self.block_durations = []  # Cache of block durations
        self.extension_numeric_idx = []  # numeric IDs of the used extensions
        self.extension_string_idx = []  # string IDs of the used extensions

    def __str__(self) -> str:
        s = "Sequence:"
        s += "\nshape_library: " + str(self.shape_library)
        s += "\nrf_library: " + str(self.rf_library)
        s += "\ngrad_library: " + str(self.grad_library)
        s += "\nadc_library: " + str(self.adc_library)
        s += "\ndelay_library: " + str(self.delay_library)
        s += "\nextensions_library: " + str(
            self.extensions_library
        )  # inserted for trigger support by mveldmann
        s += "\nrf_raster_time: " + str(self.rf_raster_time)
        s += "\ngrad_raster_time: " + str(self.grad_raster_time)
        s += "\nblock_events: " + str(len(self.block_events))
        return s

    def add_block(self, *args: SimpleNamespace) -> None:
        """
        Add a new block/multiple events to the sequence. Adds a sequence block with provided as a block structure

        See also:
        - `pypulseq.Sequence.sequence.Sequence.set_block()`
        - `pypulseq.make_adc.make_adc()`
        - `pypulseq.make_trapezoid.make_trapezoid()`
        - `pypulseq.make_sinc_pulse.make_sinc_pulse()`

        Parameters
        ----------
        args : SimpleNamespace
            Block structure or events to be added as a block to `Sequence`.
        """
        block.set_block(self, len(self.block_events) + 1, *args)

    def calculate_kspace(
        self, trajectory_delay: int = 0
    ) -> Tuple[np.array, np.array, np.array, np.array, np.array]:
        """
        Calculates the k-space trajectory of the entire pulse sequence.

        Parameters
        ----------
        trajectory_delay : int, default=0
            Compensation factor in seconds (s) to align ADC and gradients in the reconstruction.

        Returns
        -------
        k_traj_adc : numpy.array
            K-space trajectory sampled at `t_adc` timepoints.
        k_traj : numpy.array
            K-space trajectory of the entire pulse sequence.
        t_excitation : numpy.array
            Excitation timepoints.
        t_refocusing : numpy.array
            Refocusing timepoints.
        t_adc : numpy.array
            Sampling timepoints.
        """
        if np.any(np.abs(trajectory_delay) > 100e-6):
            raise Warning(
                f"Trajectory delay of {trajectory_delay * 1e6} us is suspiciously high"
            )

        # Initialise the counters and accumulator objects
        count_excitation = 0
        count_refocusing = 0
        count_adc_samples = 0

        # Loop through the blocks to prepare preallocations
        for block_counter in range(len(self.block_events)):
            block = self.get_block(block_counter + 1)
            if block.rf is not None:
                if (
                    not hasattr(block.rf, "use")
                    or block.rf.use == "excitation"
                    or block.rf.use == "undefined"
                ):
                    count_excitation += 1
                elif block.rf.use == "refocusing":
                    count_refocusing += 1

            if block.adc is not None:
                count_adc_samples += int(block.adc.num_samples)

        t_excitation = np.zeros(count_excitation)
        t_refocusing = np.zeros(count_refocusing)
        k_time = np.zeros(count_adc_samples)
        current_duration = 0
        count_excitation = 0
        count_refocusing = 0
        kc_outer = 0
        traj_recon_delay = trajectory_delay

        # Go through the blocks and collect RF and ADC timing data
        for block_counter in range(len(self.block_events)):
            block = self.get_block(block_counter + 1)

            if block.rf is not None:
                rf = block.rf
                rf_center, _ = calc_rf_center(rf)
                t = rf.delay + rf_center
                if (
                    not hasattr(block.rf, "use")
                    or block.rf.use == "excitation"
                    or block.rf.use == "undefined"
                ):
                    t_excitation[count_excitation] = current_duration + t
                    count_excitation += 1
                elif block.rf.use == "refocusing":
                    t_refocusing[count_refocusing] = current_duration + t
                    count_refocusing += 1

            if block.adc is not None:
                _k_time = np.arange(block.adc.num_samples) + 0.5
                _k_time = (
                    _k_time * block.adc.dwell
                    + block.adc.delay
                    + current_duration
                    + traj_recon_delay
                )
                k_time[kc_outer : kc_outer + block.adc.num_samples] = _k_time
                kc_outer += block.adc.num_samples
            current_duration += self.block_durations[block_counter]

        # Now calculate the actual k-space trajectory based on the gradient waveforms
        gw = self.gradient_waveforms()
        i_excitation = np.round(t_excitation / self.grad_raster_time)
        i_refocusing = np.round(t_refocusing / self.grad_raster_time)
        i_periods = np.sort(
            [1, *(i_excitation + 1), *(i_refocusing + 1), gw.shape[1] + 1]
        ).astype(np.int)
        # i_periods -= 1  # Python is 0-indexed
        ii_next_excitation = np.min((len(i_excitation), 1))
        ii_next_refocusing = np.min((len(i_refocusing), 1))
        k_traj = np.zeros_like(gw)
        k = np.zeros((3, 1))

        for i in range(len(i_periods) - 1):
            i_period_end = i_periods[i + 1] - 1
            k_period = np.concatenate(
                (k, gw[:, i_periods[i] - 1 : i_period_end] * self.grad_raster_time),
                axis=1,
            )
            k_period = np.cumsum(k_period, axis=1)
            k_traj[:, i_periods[i] - 1 : i_period_end] = k_period[:, 1:]
            k = k_period[:, -1]

            if (
                ii_next_excitation > 0
                and i_excitation[ii_next_excitation - 1] == i_period_end
            ):
                k[:] = 0
                k_traj[:, i_period_end - 1] = np.nan
                ii_next_excitation = min(len(i_excitation), ii_next_excitation + 1)

            if (
                ii_next_refocusing > 0
                and i_refocusing[ii_next_refocusing - 1] == i_period_end
            ):
                k = -k
                ii_next_refocusing = min(len(i_refocusing), ii_next_refocusing + 1)

            k = k.reshape((-1, 1))  # To be compatible with np.concatenate

        k_traj_adc = []
        for _k_traj_row in k_traj:
            result = np.interp(
                xp=np.array(range(1, k_traj.shape[1] + 1)) * self.grad_raster_time,
                fp=_k_traj_row,
                x=k_time,
            )
            k_traj_adc.append(result)
        k_traj_adc = np.stack(k_traj_adc)
        t_adc = k_time

        return k_traj_adc, k_traj, t_excitation, t_refocusing, t_adc

    def calculate_kspacePP(
        self,
        trajectory_delay: Union[int, float, np.ndarray] = 0,
        gradient_offset: int = 0,
    ) -> Tuple[np.array, np.array, np.array, np.array, np.array]:
        """
        Calculates the k-space trajectory of the entire pulse sequence.

        Parameters
        ----------
        trajectory_delay : int, default=0
            Compensation factor in seconds (s) to align ADC and gradients in the reconstruction.

        Returns
        -------
        k_traj_adc : numpy.array
            K-space trajectory sampled at `t_adc` timepoints.
        k_traj : numpy.array
            K-space trajectory of the entire pulse sequence.
        t_excitation : numpy.array
            Excitation timepoints.
        t_refocusing : numpy.array
            Refocusing timepoints.
        t_adc : numpy.array
            Sampling timepoints.
        """
        if np.any(np.abs(trajectory_delay) > 100e-6):
            raise Warning(
                f"Trajectory delay of {trajectory_delay * 1e6} us is suspiciously high"
            )

        total_duration = np.sum(self.block_durations)

        gw_data, tfp_excitation, tfp_refocusing, t_adc, _ = self.waveforms_and_times()

        ng = len(gw_data)
        # Gradient delay handling
        if isinstance(trajectory_delay, (int, float)):
            gradient_delays = [trajectory_delay] * ng
        else:
            assert (
                len(trajectory_delay) == ng
            )  # Need to have same number of gradient channels
            gradient_delays = [trajectory_delay] * ng

        # Gradient offset handling
        if isinstance(gradient_offset, (int, float)):
            gradient_offset = [gradient_offset] * ng
        else:
            assert (
                len(gradient_offset) == ng
            )  # Need to have same number of gradient channels

        # Convert data to piecewise polynomials
        gw_pp = np.empty(ng, dtype="object")
        for j in range(ng):
            wave_cnt = gw_data[j].shape[1]
            if wave_cnt == 0:
                if np.abs(gradient_offset[j]) <= eps:
                    continue
                else:
                    gw = np.array(([0, total_duration], [0, 0]))
            else:
                gw = gw_data[j]

            # Now gw contains the waveform from the current axis
            if np.abs(gradient_delays[j]) > eps:
                gw[1] = gw[1] - gradient_delays[j]  # Anisotropic gradient delay support
            if not np.all(np.isfinite(gw)):
                raise Warning("Not all elements of the generated waveform are finite.")

            teps = 1e-12
            if gw[0, 0] > 0 and gw[1, -1] < total_duration:
                # teps terms to avoid integration errors over extended periods of time
                _temp1 = np.array(([-teps, gw[0, 0] - teps], [0, 0]))
                _temp2 = np.array(([gw[0, -1] + teps, total_duration + teps], [0, 0]))
                gw = np.hstack((_temp1, gw, _temp2))
            elif gw[0, 0] > 0:
                _temp = np.array(([-teps, gw[0, 0] - teps], [0, 0]))
                gw = np.hstack((_temp, gw))
            elif gw[0, -1] < total_duration:
                _temp = np.array(([gw[0, -1] + teps, total_duration + teps], [0, 0]))
                gw = np.hstack((gw, _temp))

            if np.abs(gradient_offset[j]) > eps:
                gw[1:] += gradient_offset[j]

            # gw_pp[j] = interpolate.interp1d(x=gw[0], y=gw[1], kind='linear')
            # _c = np.vstack((gw[1, :-1], gw[1, :-1]))  # Construct of shape (2, ...) to indicate 2nd degree polynomial
            _c = gw[1, :-1].reshape((-1, 1))
            gw_pp[j] = interpolate.PPoly(x=gw[0], c=_c)

        # Calculate slice positions. for now we entirely rely on the
        # Excitation -- ignoring complicated interleaved refocused sequences
        slice_pos = np.zeros(
            (len(gw_data), tfp_excitation.shape[1])
        )  # Position in x, y, z
        for j in range(len(gw_data)):
            if gw_pp[j] is None:
                slice_pos[j] = np.empty_like((1, slice_pos.shape[1]))
            else:
                slice_pos[j] = tfp_excitation[1] / gw_pp[j](tfp_excitation[0])
        slice_pos[~np.isfinite(slice_pos)] = 0  # Reset undefined to 0
        t_slice_pos = tfp_excitation[0]

        # Integrate waveforms as PPs to produce gradient moments
        gm_pp = np.empty(ng)
        tc = []
        for i in range(ng):
            if gw_pp[i] is None:
                continue

            gm_pp[i] = gw_pp[i].antiderivative()
            tc.append(gm_pp[i].x)
            # ii = np.where(np.abs(gm_pp[i].c[]))

        # TODO Complete

        return k_traj_adc, k_traj, t_excitation, t_refocusing, t_adc

    def check_timing(self) -> Tuple[bool, List[str]]:
        """
        Checks timing of all blocks and objects in the sequence optionally returns the detailed error log. This
        function also modifies the sequence object by adding the field "TotalDuration" to sequence definitions.

        Returns
        -------
        is_ok : bool
            Boolean flag indicating timing errors.
        error_report : str
            Error report in case of timing errors.
        """
        error_report = []
        is_ok = True
        num_blocks = len(self.block_events)
        total_duration = 0

        for block_counter in range(num_blocks):
            block = self.get_block(block_counter + 1)
            events = [e for e in vars(block).values() if e is not None]
            res, rep, duration = ext_check_timing(self.system, *events)
            is_ok = is_ok and res

            # Check the stored total block duration
            if np.abs(duration - self.block_durations[block_counter]) > eps:
                rep += "Inconsistency between the stored block duration and the duration of the block content"
                is_ok = False
                duration = self.block_durations[block_counter]

            # Check RF dead times
            if block.rf is not None:
                if block.rf.delay - block.rf.dead_time < -eps:
                    rep += (
                        f"Delay of {block.rf.delay * 1e6} us is smaller than the RF dead time "
                        f"{block.rf.dead_time * 1e6} us"
                    )
                    is_ok = False

                if (
                    block.rf.delay + block.rf.t[-1] + block.rf.ringdown_time - duration
                    > eps
                ):
                    rep += (
                        f"Time between the end of the RF pulse at {block.rf.delay + block.rf.t[-1]} and the end "
                        f"of the block at {duration * 1e6} us is shorter than rf_ringdown_time"
                    )
                    is_ok = False

            # Check ADC dead times
            if block.adc is not None:
                if block.adc.delay - self.system.adc_dead_time < -eps:
                    rep += "adc.delay < system.adc_dead_time"
                    is_ok = False

                if (
                    block.adc.delay
                    + block.adc.num_samples * block.adc.dwell
                    + self.system.adc_dead_time
                    - duration
                    > eps
                ):
                    rep += "adc: system.adc_dead_time (post-ADC) violation"
                    is_ok = False

            # Update report
            if len(rep) != 0:
                error_report.append(f"Event: {block_counter} - {rep}\n")
            total_duration += duration

        # Check if all the gradients in the last block are ramped down properly
        if len(events) != 0 and all([isinstance(e, SimpleNamespace) for e in events]):
            for e in range(len(events)):
                if not isinstance(events[e], list) and events[e].type == "grad":
                    if events[e].last != 0:
                        error_report.append(
                            f"Event {num_blocks - 1} gradients do not ramp to 0 at the end of the sequence"
                        )

        self.set_definition("TotalDuration", total_duration)

        return is_ok, error_report

    def duration(self) -> Tuple[int, int, np.ndarray]:
        """
        Returns the total duration of this sequence, and the total count of blocks and events.

        Returns
        -------
        duration : int
            Duration of this sequence in seconds (s).
        num_blocks : int
            Number of blocks in this sequence.
        event_count : np.ndarray
            Number of events in this sequence.
        """
        num_blocks = len(self.block_events)
        event_count = np.zeros(len(self.block_events[1]))
        duration = 0
        for block_counter in range(num_blocks):
            event_count += self.block_events[block_counter + 1] > 0
            duration += self.block_durations[block_counter]

        return duration, num_blocks, event_count

    def flip_grad_axis(self, axis: str) -> None:
        """
        Invert all gradients along the corresponding axis/channel. The function acts on all gradient objects already
        added to the sequence object.

        Parameters
        ----------
        axis : str
            Gradients to invert or scale. Must be one of 'x', 'y' or 'z'.
        """
        self.mod_grad_axis(axis, modifier=-1)

    def get_block(self, block_index: int) -> SimpleNamespace:
        """
        Return a block of the sequence  specified by the index. The block is created from the sequence data with all
        events and shapes decompressed.

        See also:
        - `pypulseq.Sequence.sequence.Sequence.set_block()`.
        - `pypulseq.Sequence.sequence.Sequence.add_block()`.

        Parameters
        ----------
        block_index : int
            Index of block to be retrieved from `Sequence`.

        Returns
        -------
        SimpleNamespace
            Event identified by `block_index`.
        """
        return block.get_block(self, block_index)

    def get_definition(self, key: str) -> str:
        """
        Return value of the definition specified by the key. These definitions can be added manually or read from the
        header of a sequence file defined in the sequence header. An empty array is returned if the key is not defined.

        See also `pypulseq.Sequence.sequence.Sequence.set_definition()`.

        Parameters
        ----------
        key : str
            Key of definition to retrieve.

        Returns
        -------
        str
            Definition identified by `key` if found, else returns ''.
        """
        if key in self.definitions:
            return self.definitions[key]
        else:
            return ""

    def get_extension_type_ID(self, extension_string: str) -> int:
        """
        Get numeric extension ID for `extension_string`. Will automatically create a new ID if unknown.

        Parameters
        ----------
        extension_string : str
            Given string extension ID.

        Returns
        -------
        extension_id : int
            Numeric ID for given string extension ID.

        """
        if extension_string not in self.extension_string_idx:
            if len(self.extension_numeric_idx) == 0:
                extension_id = 1
            else:
                extension_id = 1 + max(self.extension_numeric_idx)

            self.extension_numeric_idx.append(extension_id)
            self.extension_string_idx.append(extension_string)
            assert len(self.extension_numeric_idx) == len(self.extension_string_idx)
        else:
            num = self.extension_string_idx.index(extension_string)
            extension_id = self.extension_numeric_idx[num]

        return extension_id

    def get_extension_type_string(self, extension_id: int) -> str:
        """
        Get string extension ID for `extension_id`.

        Parameters
        ----------
        extension_id : int
            Given numeric extension ID.

        Returns
        -------
        extension_str : str
            String ID for the given numeric extension ID.

        Raises
        ------
        ValueError
            If given numeric extension ID is unknown.
        """
        if extension_id in self.extension_numeric_idx:
            num = self.extension_numeric_idx.index(extension_id)
        else:
            raise ValueError(
                f"Extension for the given ID - {extension_id} - is unknown."
            )

        extension_str = self.extension_string_idx[num]
        return extension_str

    def gradient_waveforms(self) -> np.ndarray:
        """
        Decompress the entire gradient waveform. Returns an array of shape `gradient_axes x timepoints`.
        `gradient_axes` is typically 3.

        Returns
        -------
        grad_waveforms : np.ndarray
            Decompressed gradient waveform.
        """
        duration, num_blocks, _ = self.duration()

        wave_length = np.ceil(duration / self.grad_raster_time).astype(int)
        grad_channels = 3
        grad_waveforms = np.zeros((grad_channels, wave_length))
        grad_channels = ["gx", "gy", "gz"]

        t0 = 0
        t0_n = 0
        for block_counter in range(num_blocks):
            block = self.get_block(block_counter + 1)
            for j in range(len(grad_channels)):
                grad = getattr(block, grad_channels[j])
                if grad is not None:
                    if grad.type == "grad":
                        nt_start = np.round(grad.delay / self.grad_raster_time)
                        waveform = grad.waveform
                    else:
                        nt_start = np.round(grad.delay / self.grad_raster_time)
                        if np.abs(grad.flat_time) > eps:
                            t = np.cumsum(
                                [0, grad.rise_time, grad.flat_time, grad.fall_time]
                            )
                            trap_form = np.array([0, 1, 1, 0]) * grad.amplitude
                        else:
                            t = np.cumsum([0, grad.rise_time, grad.fall_time])
                            trap_form = np.array([0, 1, 0]) * grad.amplitude

                        tn = math.floor(t[-1] / self.grad_raster_time)
                        t = np.append(t, t[-1] + self.grad_raster_time)
                        trap_form = np.append(trap_form, 0)

                        if np.abs(grad.amplitude) > eps:
                            waveform = points_to_waveform(
                                times=t,
                                amplitudes=trap_form,
                                grad_raster_time=self.grad_raster_time,
                            )
                        else:
                            waveform = np.zeros(tn + 1)

                    if len(waveform) != np.sum(np.isfinite(waveform)):
                        warn("Not all elements of the generated waveform are finite")

                    """
                    Matlab dynamically resizes arrays during slice assignment operation if assignment is out of bounds
                    Numpy does not; following is a workaround
                    """
                    l1, l2 = int(t0_n + nt_start), int(t0_n + nt_start + len(waveform))
                    if l2 > grad_waveforms.shape[1]:
                        z = np.zeros(
                            (grad_waveforms.shape[0], l2 - grad_waveforms.shape[1])
                        )
                        grad_waveforms = np.hstack((grad_waveforms, z))
                    grad_waveforms[j, l1:l2] = waveform

            t0 += self.block_durations[block_counter]
            t0_n = np.round(t0 / self.grad_raster_time)

        return grad_waveforms

    def mod_grad_axis(self, axis: str, modifier: int) -> None:
        """
        Invert or scale all gradients along the corresponding axis/channel. The function acts on all gradient objects
        already added to the sequence object.

        Parameters
        ----------
        axis : str
            Gradients to invert or scale. Must be one of 'x', 'y' or 'z'.
        modifier : int
            Scaling value.

        Raises
        ------
        ValueError
            If invalid `axis` is passed. Must be one of 'x', 'y','z'.
        RuntimeError
            If same gradient event is used on multiple axes.
        """
        if axis not in ["x", "y", "z"]:
            raise ValueError(
                f"Invalid axis. Must be one of 'x', 'y','z'. Passed: {axis}"
            )

        channel_num = ["x", "y", "z"].index(axis)
        other_channels = [0, 1, 2]
        other_channels.remove(channel_num)

        # Go through all event table entries and list gradient objects in the library
        all_grad_events = np.array(list(self.block_events.values()))
        all_grad_events = all_grad_events[:, 2:5]

        selected_events = np.unique(all_grad_events[:, channel_num])
        selected_events = selected_events[selected_events != 0]
        other_events = np.unique(all_grad_events[:, other_channels])
        if len(np.intersect1d(selected_events, other_events)) > 0:
            raise RuntimeError(
                "mod_grad_axis does not yet support the same gradient event used on multiple axes."
            )

        for i in range(len(selected_events)):
            self.grad_library.data[selected_events[i]][0] *= modifier
            if (
                self.grad_library.type[selected_events[i]] == "g"
                and self.grad_library.lengths[selected_events[i]] == 5
            ):
                # Need to update first and last fields
                self.grad_library.data[selected_events[i]][3] *= modifier
                self.grad_library.data[selected_events[i]][4] *= modifier

    def plot(
        self,
        label: str = str(),
        show_blocks: bool = False,
        save: bool = False,
        time_range=(0, np.inf),
        time_disp: str = "s",
    ) -> None:
        """
        Plot `Sequence`.

        Parameters
        ----------
        label : str, defualt=str()
            Plot label values for ADC events: in this example for LIN and REP labels; other valid labes are accepted as
            a comma-separated list.
        save : bool, default=False
            Boolean flag indicating if plots should be saved. The two figures will be saved as JPG with numerical
            suffixes to the filename 'seq_plot'.
        show_blocks : bool, default=False
            Boolean flag to indicate if grid and tick labels at the block boundaries are to be plotted.
        time_range : iterable, default=(0, np.inf)
            Time range (x-axis limits) for plotting the sequence. Default is 0 to infinity (entire sequence).
        time_disp : str, default='s'
            Time display type, must be one of `s`, `ms` or `us`.
        plot_type : str, default='Gradient'
            Gradients display type, must be one of either 'Gradient' or 'Kspace'.
        """
        mpl.rcParams["lines.linewidth"] = 0.75  # Set default Matplotlib linewidth

        valid_time_units = ["s", "ms", "us"]
        valid_labels = get_supported_labels()
        if (
            not all([isinstance(x, (int, float)) for x in time_range])
            or len(time_range) != 2
        ):
            raise ValueError("Invalid time range")
        if time_disp not in valid_time_units:
            raise ValueError("Unsupported time unit")

        fig1, fig2 = plt.figure(1), plt.figure(2)
        sp11 = fig1.add_subplot(311)
        sp12 = fig1.add_subplot(312, sharex=sp11)
        sp13 = fig1.add_subplot(313, sharex=sp11)
        fig2_subplots = [
            fig2.add_subplot(311, sharex=sp11),
            fig2.add_subplot(312, sharex=sp11),
            fig2.add_subplot(313, sharex=sp11),
        ]

        t_factor_list = [1, 1e3, 1e6]
        t_factor = t_factor_list[valid_time_units.index(time_disp)]

        t0 = 0
        label_defined = False
        label_idx_to_plot = []
        label_legend_to_plot = []
        label_store = dict()
        for i in range(len(valid_labels)):
            label_store[valid_labels[i]] = 0
            if valid_labels[i] in label.upper():
                label_idx_to_plot.append(i)
                label_legend_to_plot.append(valid_labels[i])

        if len(label_idx_to_plot) != 0:
            p = parula.main(len(label_idx_to_plot) + 1)
            label_colors_to_plot = p(np.arange(len(label_idx_to_plot)))
            label_colors_to_plot = np.roll(label_colors_to_plot, -1, axis=0).tolist()

        # Block timings
        block_edges = np.cumsum([0, *self.block_durations])
        block_edges_in_range = block_edges[
            (block_edges >= time_range[0]) * (block_edges <= time_range[1])
        ]
        if show_blocks:
            for sp in [sp11, sp12, sp13, *fig2_subplots]:
                sp.set_xticks(t_factor * block_edges_in_range)
                sp.set_xticklabels(rotation=90)

        for block_counter in range(len(self.block_events)):
            block = self.get_block(block_counter + 1)
            is_valid = time_range[0] <= t0 <= time_range[1]
            if is_valid:
                if getattr(block, "label", None) is not None:
                    for i in range(len(block.label)):
                        if block.label[i].type == "labelinc":
                            label_store[block.label[i].label] += block.label[i].value
                        else:
                            label_store[block.label[i].label] = block.label[i].value
                    label_defined = True

                if getattr(block, "adc", None) is not None:  # ADC
                    adc = block.adc
                    # From Pulseq: According to the information from Klaus Scheffler and indirectly from Siemens this
                    # is the present convention - the samples are shifted by 0.5 dwell
                    t = adc.delay + (np.arange(int(adc.num_samples)) + 0.5) * adc.dwell
                    sp11.plot(t_factor * (t0 + t), np.zeros(len(t)), "rx")
                    sp13.plot(
                        t_factor * (t0 + t),
                        np.angle(
                            np.exp(1j * adc.phase_offset)
                            * np.exp(1j * 2 * np.pi * t * adc.freq_offset)
                        ),
                        "b.",
                        markersize=0.25,
                    )

                    if label_defined and len(label_idx_to_plot) != 0:
                        cycler = mpl.cycler(color=label_colors_to_plot)
                        sp11.set_prop_cycle(cycler)
                        label_colors_to_plot = np.roll(
                            label_colors_to_plot, -1, axis=0
                        ).tolist()
                        arr_label_store = list(label_store.values())
                        lbl_vals = np.take(arr_label_store, label_idx_to_plot)
                        t = t0 + adc.delay + (adc.num_samples - 1) / 2 * adc.dwell
                        _t = [t_factor * t] * len(lbl_vals)
                        # Plot each label individually to retrieve each corresponding Line2D object
                        p = itertools.chain.from_iterable(
                            [
                                sp11.plot(__t, _lbl_vals, ".")
                                for __t, _lbl_vals in zip(_t, lbl_vals)
                            ]
                        )
                        if len(label_legend_to_plot) != 0:
                            sp11.legend(p, label_legend_to_plot, loc="upper left")
                            label_legend_to_plot = []

                if getattr(block, "rf", None) is not None:  # RF
                    rf = block.rf
                    tc, ic = calc_rf_center(rf)
                    time = rf.t
                    signal = rf.signal
                    if np.abs(signal[0]) != 0:
                        signal = np.insert(signal, obj=0, values=0)
                        time = np.insert(time, obj=0, values=time[0])
                        ic += 1

                    if np.abs(signal[-1]) != 0:
                        signal = np.append(signal, 0)
                        time = np.append(time, time[-1])

                    sp12.plot(t_factor * (t0 + time + rf.delay), np.abs(signal))
                    sp13.plot(
                        t_factor * (t0 + time + rf.delay),
                        np.angle(
                            signal
                            * np.exp(1j * rf.phase_offset)
                            * np.exp(1j * 2 * math.pi * time * rf.freq_offset)
                        ),
                        t_factor * (t0 + tc + rf.delay),
                        np.angle(
                            signal[ic]
                            * np.exp(1j * rf.phase_offset)
                            * np.exp(1j * 2 * math.pi * time[ic] * rf.freq_offset)
                        ),
                        "xb",
                    )

                grad_channels = ["gx", "gy", "gz"]
                for x in range(len(grad_channels)):  # Gradients
                    if getattr(block, grad_channels[x], None) is not None:
                        grad = getattr(block, grad_channels[x])
                        if grad.type == "grad":
                            # We extend the shape by adding the first and the last points in an effort of making the
                            # display a bit less confusing...
                            time = grad.delay + [0, *grad.tt, grad.shape_dur]
                            waveform = 1e-3 * np.array(
                                (grad.first, *grad.waveform, grad.last)
                            )
                        else:
                            time = np.cumsum(
                                [
                                    0,
                                    grad.delay,
                                    grad.rise_time,
                                    grad.flat_time,
                                    grad.fall_time,
                                ]
                            )
                            waveform = 1e-3 * grad.amplitude * np.array([0, 0, 1, 1, 0])
                        fig2_subplots[x].plot(t_factor * (t0 + time), waveform)
            t0 += self.block_durations[block_counter]

        grad_plot_labels = ["x", "y", "z"]
        sp11.set_ylabel("ADC")
        sp12.set_ylabel("RF mag (Hz)")
        sp13.set_ylabel("RF/ADC phase (rad)")
        sp13.set_xlabel("t(s)")
        for x in range(3):
            _label = grad_plot_labels[x]
            fig2_subplots[x].set_ylabel(f"G{_label} (kHz/m)")
        fig2_subplots[-1].set_xlabel("t(s)")

        # Setting display limits
        disp_range = t_factor * np.array([time_range[0], min(t0, time_range[1])])
        [x.set_xlim(disp_range) for x in [sp11, sp12, sp13, *fig2_subplots]]

        # Grid on
        for sp in [sp11, sp12, sp13, *fig2_subplots]:
            sp.grid()

        fig1.tight_layout()
        fig2.tight_layout()
        if save:
            fig1.savefig("seq_plot1.jpg")
            fig2.savefig("seq_plot2.jpg")
        plt.show()

    def read(self, file_path: str, detect_rf_use: bool = False) -> None:
        """
        Read `.seq` file from `file_path`.

        Parameters
        ----------
        detect_rf_use
        file_path : str
            Path to `.seq` file to be read.
        """
        read(self, path=file_path, detect_rf_use=detect_rf_use)

    def register_adc_event(self, event: EventLibrary) -> int:
        return block.register_adc_event(self, event)

    def register_grad_event(
        self, event: SimpleNamespace
    ) -> Union[int, Tuple[int, int]]:
        return block.register_grad_event(self, event)

    def register_label_event(self, event: SimpleNamespace) -> int:
        return block.register_label_event(self, event)

    def register_rf_event(self, event: SimpleNamespace) -> Tuple[int, List[int]]:
        return block.register_rf_event(self, event)

    def rf_from_lib_data(self, lib_data: list, use: str = str()) -> SimpleNamespace:
        """
        Construct RF object from `lib_data`.

        Parameters
        ----------
        lib_data : list
            RF envelope.
        use : str, default=str()
            RF event use.

        Returns
        -------
        rf : SimpleNamespace
            RF object constructed from `lib_data`.
        """
        rf = SimpleNamespace()
        rf.type = "rf"

        amplitude, mag_shape, phase_shape = lib_data[0], lib_data[1], lib_data[2]
        shape_data = self.shape_library.data[mag_shape]
        compressed = SimpleNamespace()
        compressed.num_samples = shape_data[0]
        compressed.data = shape_data[1:]
        mag = decompress_shape(compressed)
        shape_data = self.shape_library.data[phase_shape]
        compressed.num_samples = shape_data[0]
        compressed.data = shape_data[1:]
        phase = decompress_shape(compressed)
        rf.signal = amplitude * mag * np.exp(1j * 2 * np.pi * phase)
        time_shape = lib_data[3]
        if time_shape > 0:
            shape_data = self.shape_library.data[time_shape]
            compressed.num_samples = shape_data[0]
            compressed.data = shape_data[1:]
            rf.t = decompress_shape(compressed) * self.rf_raster_time
            rf.shape_dur = (
                np.ceil((rf.t[-1] - eps) / self.rf_raster_time) * self.rf_raster_time
            )
        else:  # Generate default time raster on the fly
            rf.t = (np.arange(1, len(rf.signal) + 1) - 0.5) * self.rf_raster_time
            rf.shape_dur = len(rf.signal) * self.rf_raster_time

        rf.delay = lib_data[4]
        rf.freq_offset = lib_data[5]
        rf.phase_offset = lib_data[6]

        rf.dead_time = self.system.rf_dead_time
        rf.ringdown_time = self.system.rf_ringdown_time

        if use != "":
            use_cases = {
                "e": "excitation",
                "r": "refocusing",
                "i": "inversion",
                "s": "saturation",
                "p": "preparation",
            }
            rf.use = use_cases[use] if use in use_cases else "undefined"

        return rf

    def set_block(self, block_index: int, *args: SimpleNamespace) -> None:
        """
        Replace block at index with new block provided as block structure, add sequence block, or create a new block
        from events and store at position specified by index. The block or events are provided in uncompressed form and
        will be stored in the compressed, non-redundant internal libraries.

        See also:
        - `pypulseq.Sequence.sequence.Sequence.get_block()`
        - `pypulseq.Sequence.sequence.Sequence.add_block()`

        Parameters
        ----------
        block_index : int
            Index at which block is replaced.
        args : SimpleNamespace
            Block or events to be replaced/added or created at `block_index`.
        """
        block.set_block(self, block_index, *args)

    def set_definition(
        self, key: str, value: Union[float, int, list, np.ndarray, str, tuple]
    ) -> None:
        """
        Modify a custom definition of the sequence. Set the user definition 'key' to value 'value'. If the definition
        does not exist it will be created.

        See also `pypulseq.Sequence.sequence.Sequence.get_definition()`.

        Parameters
        ----------
        key : str
            Definition key.
        value : int, list, np.ndarray, str or tuple
            Definition value.
        """
        if key == "FOV":
            if np.max(value) > 1:
                text = "Definition FOV uses values exceeding 1 m. "
                text += "New Pulseq interpreters expect values in units of meters."
                warn(text)

        self.definitions[key] = value

    def set_extension_string_ID(self, extension_str: str, extension_id: int) -> None:
        """
        Set numeric ID for the given string extension ID.

        Parameters
        ----------
        extension_str : str
            Given string extension ID.
        extension_id : int
            Given numeric extension ID.

        Raises
        ------
        ValueError
            If given numeric or string extension ID is not unique.
        """
        if (
            extension_str in self.extension_string_idx
            or extension_id in self.extension_numeric_idx
        ):
            raise ValueError("Numeric or string ID is not unique")

        self.extension_numeric_idx.append(extension_id)
        self.extension_string_idx.append(extension_str)
        assert len(self.extension_numeric_idx) == len(self.extension_string_idx)

    def test_report(self) -> str:
        """
        Analyze the sequence and return a text report.
        """
        return ext_test_report(self)

    def waveforms_and_times(
        self, append_RF: bool = False
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Decompress the entire gradient waveform. Returns gradient waveforms as a tuple of `np.ndarray` of
        `gradient_axes` (typically 3) dimensions. Each `np.ndarray` contains timepoints and the corresponding
        gradient amplitude values. Additional return values are time points of excitations, refocusings and ADC
        sampling points.

        Parameters
        ----------
        append_RF : bool, default=False
            Boolean flag to indicate if RF wave shapes are to be appended after the gradients.

        Returns
        -------
        wave_data : np.ndarray
        tfp_excitation : np.ndarray
            Contains time moments, frequency and phase offsets of the excitation RF pulses (similar for `
            tfp_refocusing`).
        t_adc: np.ndarray
            Contains times of all ADC sample points.
        fp_adc : np.ndarray
            Contains frequency and phase offsets of each ADC object (not samples).
        """
        grad_channels = ["gx", "gy", "gz"]

        num_blocks = len(self.block_events)

        # Collect shape pieces
        if append_RF:
            shape_channels = len(grad_channels) + 1  # Last 'channel' is RF
        else:
            shape_channels = len(grad_channels)

        shape_pieces = np.empty((shape_channels, num_blocks), dtype="object")
        # Also collect RF and ADC timing data
        # t_excitation, t_refocusing, t_adc
        tfp_excitation = []
        tfp_refocusing = []
        t_adc = []
        fp_adc = []

        curr_dur = 0
        out_len = np.zeros(shape_channels)  # Last 'channel' is RF

        for block_counter in range(num_blocks):
            block = self.get_block(block_counter + 1)
            for j in range(len(grad_channels)):
                grad = getattr(block, grad_channels[j])
                if grad is not None:  # Gradients
                    if grad.type == "grad":
                        # Check if we have an extended trapezoid or an arbitrary gradient on a regular raster
                        tt_rast = grad.tt / self.grad_raster_time + 0.5  # TODO
                        if np.all(
                            np.abs(tt_rast - np.arange(1, len(tt_rast) + 1)) < eps
                        ):  # Arbitrary gradient
                            """
                            Arbitrary gradient: restore & recompress shape - if we had a trapezoid converted to shape we
                            have to find the "corners" and we can eliminate internal samples on the straight segments
                            but first we have to restore samples on the edges of the gradient raster intervals for that
                            we need the first sample.
                            """
                            max_abs = np.max(np.abs(grad.waveform))
                            odd_step1 = np.array([grad.first, *(2 * grad.waveform)])
                            odd_step2 = odd_step1 * (
                                np.mod(np.arange(1, len(odd_step1) + 1), 2) * 2 - 1
                            )
                            waveform_odd_rest = np.cumsum(odd_step2) * (
                                np.mod(np.arange(1, len(odd_step2)), 2) * 2 - 1
                            )
                        else:  # Extended trapezoid
                            out_len[j] += len(grad.tt)
                            shape_pieces[j, block_counter] = [
                                [curr_dur + grad.delay + grad.tt],
                                [grad.waveform],
                            ]
                    else:
                        if np.abs(grad.flat_time) > eps:
                            out_len[j] += 4
                            _temp = np.vstack(
                                (
                                    curr_dur
                                    + grad.delay
                                    + np.cumsum(
                                        [
                                            0,
                                            grad.rise_time,
                                            grad.flat_time,
                                            grad.fall_time,
                                        ]
                                    ),
                                    grad.amplitude * np.array([0, 1, 1, 0]),
                                )
                            )
                            shape_pieces[j, block_counter] = _temp
                        else:
                            out_len[j] += 3
                            _temp = np.vstack(
                                (
                                    curr_dur
                                    + grad.delay
                                    + np.cumsum([0, grad.rise_time, grad.fall_time]),
                                    grad.amplitude * np.array([0, 1, 0]),
                                )
                            )
                            shape_pieces[j, block_counter] = _temp
            if block.rf is not None:  # RF
                rf = block.rf
                t = rf.delay + calc_rf_center(rf)[0]
                if not hasattr(rf, "use") or block.rf.use in [
                    "excitation",
                    "undefined",
                ]:
                    tfp_excitation.append(
                        [curr_dur + t, block.rf.freq_offset, block.rf.phase_offset]
                    )
                elif block.rf.use == "refocusing":
                    tfp_refocusing.append(
                        [curr_dur + t, block.rf.freq_offset, block.rf.phase_offset]
                    )
                if append_RF:
                    pre = []
                    post = []
                    if np.abs(rf.signal[0]) > 0:
                        pre = np.array([[curr_dur + rf.delay + rf.t[-1] - eps], [0]])

                    if np.abs(rf.signal[-1]) > 0:
                        post = np.array([[curr_dur + rf.delay + rf.t[-1] + eps], [0]])

                    out_len[-1] += len(rf.t) + pre.shape[1] + post.shape[1]
                    shape_pieces[-1, block_counter] = np.hstack(
                        (pre, [[curr_dur + rf.delay + rf.signal], rf.signal], post)
                    )
            if block.adc is not None:  # ADC
                t_adc.extend(
                    np.arange(block.adc.num_samples)
                    + 0.5 * block.adc.dwell
                    + block.adc.delay
                    + curr_dur
                )
                fp_adc.append([block.adc.freq_offset, block.adc.phase_offset])

            curr_dur += self.block_durations[block_counter]

        # Collect wave data
        wave_data = np.empty(shape_channels, dtype="object")
        for j in range(shape_channels):
            wave_data[j] = np.zeros((2, int(out_len[j])))
        wave_cnt = np.zeros(shape_channels, dtype=int)
        for block_counter in range(num_blocks):
            for j in range(shape_channels):
                if shape_pieces[j, block_counter] is not None:
                    wave_data_local = shape_pieces[j, block_counter]
                    length = wave_data_local.shape[1]
                    if (
                        wave_cnt[j] == 0
                        or wave_data[j][0, wave_cnt[j] - 1] != wave_data_local[0, 0]
                    ):
                        wave_data[j][
                            :, wave_cnt[j] + np.arange(length)
                        ] = wave_data_local
                        wave_cnt[j] += length
                    else:
                        wave_data[j][
                            :, wave_cnt[j] + np.arange(length - 1)
                        ] = wave_data_local[:, 1:]
                        wave_cnt[j] += length - 1
                    if wave_cnt[j] != len(np.unique(wave_data[j][0, : wave_cnt[j]])):
                        raise Warning(
                            "Not all elements of the generated time vector are unique."
                        )

        # Trim output data
        for j in range(shape_channels):
            if wave_cnt[j] < wave_data[j].shape[1]:
                wave_data[j] = wave_data[j][:, : wave_cnt[j]]

        tfp_excitation = np.array(tfp_excitation).transpose()
        tfp_refocusing = np.array(tfp_refocusing)
        t_adc = np.array(t_adc)
        fp_adc = np.array(fp_adc)

        return wave_data, tfp_excitation, tfp_refocusing, t_adc, fp_adc

    def write(self, name: str, create_signature: bool = True) -> None:
        """
        Write the sequence data to the given filename using the open file format for MR sequences.

        See also `pypulseq.Sequence.read_seq.read()`.

        Parameters
        ----------
        name : str
            Filename of `.seq` file to be written to disk.
        create_signature : bool, default=True
            Boolean flag to indicate if the file has to be signed.
        """
        write_seq(self, name, create_signature)
