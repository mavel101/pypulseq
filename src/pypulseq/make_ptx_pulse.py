from types import SimpleNamespace

from pypulseq.opts import Opts


def make_ptx_pulse(flip_angle: float, 
                   rf_type: str = 'exc', 
                   delay: float = 0, 
                   duration: float = 0, 
                   freq_offset: float = 0, 
                   phase_offset: float = 0, 
                   slice_ix: int = 0, 
                   no_rot: bool = False,
                   system: Opts = None) -> SimpleNamespace:
    """
    Creates a pTx dummy pulse for the pTx extension.

    Parameters
    ----------
    flip_angle: Flip angle [deg]
    rf_type: str, default='exc'
        Pulse type. Currently only excitation ('exc') or refocusing ('ref')
    delay : float, optional, default=0
        Delay, [s].
    duration : float, optional, default=0
        Duration of pTx pulse [s]
    freq_offset: float, optional, default=0
        Frequency offset [Hz]
    phase_offset: float, optional, default=0
        Phase offset [rad]
    slice_ix: int, optional, default=0
        Slice index for slice-selective pTx pulses
        This is needed for correct slice positioning/frequency offset calculation.
        Important: Slice thickness & number of slices have to be provided in the definitions section.
    no_rot: bool, default= False
        do not rotate gradients of this pTx pulse
    system : Opts, optional, default=Opts()
        System limits.

    Returns
    ------
    pulse : SimpleNamespace
        Dummy pTx pulse event.

    Raises
    ------
    ValueError
        If `channel` is invalid. Must be one of 'osc0','osc1', or 'ext1'.
    """
    if rf_type not in ['exc', 'ref', 'sat']:
        raise ValueError(f"RF type {rf_type} is invalid. Must be one of 'exc', 'ref' or 'sat'.")

    if system == None:
        system = Opts.default

    rf_types = {'exc': 1, 'ref': 2, 'sat': 3}

    if rf_type == 'sat' and slice_ix != 0:
        slice_ix = 0
        print('Warning: Only nonselective saturation pulse allowed. Setting slice index to 0.')

    pulse = SimpleNamespace()
    pulse.type = 'ptx'
    pulse.rf_type = rf_types[rf_type]
    pulse.flip_angle = flip_angle
    pulse.delay = delay
    pulse.shape_dur = duration
    pulse.freq_offset = freq_offset
    pulse.phase_offset = phase_offset
    pulse.slice_ix = slice_ix
    pulse.no_rot = int(no_rot)
    pulse.ringdown_time = system.rf_ringdown_time
    pulse.dead_time = system.rf_dead_time

    if pulse.dead_time > pulse.delay:
        pulse.delay = pulse.dead_time

    return pulse
