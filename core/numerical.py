import numpy as np
import itertools
from functools import partial
from numpy import fft
import scipy.signal as sps

from openseize import producer
from openseize.core.producer import Producer, as_producer, pad_producer 
from openseize.core.queues import FIFOArray
from openseize.core.arraytools import pad_along_axis, slice_along_axis


def optimal_nffts(arr):
    """Estimates the optimal number of FFT points for an arr."""

    return int(8 * 2 ** np.ceil(np.log2(len(arr))))


def convolve_slicer(arr, shape1, shape2, mode, axis):
    """Applies a boundary mode to slice a convolved array along axis.

    Args:
        arr: ndarray
            A convolved ndimensional array.
        shape1: tuple
            Shape of the first input used to construct the convolved arr.
        shape2: tuple
            Shape of the second input used to construct the convolved arr.
        mode: str one of 'full', 'same', or 'valid'
            A string defining the boundary handling mode. These modes
            are the same as numpy's np.convolve mode argument.
        axis: int
            The convolution axis in arr.

    Returns:
        An ndarray with the convolve mode applied.
    """

    m, n = shape1[axis], shape2[axis]
    p, q = max(m,n), min(m,n)

    if mode == 'full':

        # full mode length is m + n - 1
        return arr
    
    if mode == 'same':
        
        # same mode length is max(m,  n) centered along axis of arr
        start = (q - 1) // 2
        stop = start + p
        return slice_along_axis(arr, start, stop, axis=axis)

    elif mode == 'valid':
       
        # valid mode length is m + n - 1 - 2 * (q-1) centered along axis
        start= q - 1 
        stop = (n + m - 1) - (q - 1)
        return slice_along_axis(arr, start, stop, axis=axis)


def _oa_mode(segment, idx, win_len, axis, mode):
    """Applies the numpy/scipy mode to the first and last segement of the
    oaconvolve generator.

    Args:
        segment (arr):          array of values yielded by oaconvolve
        idx (int):              index number of the segment
        total (int):            total number of segments 
        win_len (int):          len of convolving window
        axis (int):             axis along which convolution was applied
        mode (str):             one of 'full', 'same', 'valid'.
                                identical to numpy convovle mode
    
    Returns: segment with boundary mode applied
    """

    #FIXME refactor to reuse your convolve slicer

    modes = ('full', 'same', 'valid')
    if mode not in modes:
        msg = 'mode {} is not one of the valid modes {}'
        raise ValueError(msg.format(mode, modes))

    ns = segment.shape[axis]
    #dict of slices to apply to axis for 0th & last segment for each mode
    cuts = {'full': [slice(None), slice(None)],
            'same': [slice((win_len - 1) // 2, None), 
                     slice(None, ns - ((win_len - 1) // 2))],
            'valid': [slice(win_len - 1, None), 
                      slice(None, ns - win_len + 1)]}
    
    #apply slices
    slices = [slice(None)] * segment.ndim
    slices[axis] = cuts[mode][1] if idx > 0 else cuts[mode][0]
    return segment[tuple(slices)]


@as_producer
def oaconvolve(pro, win, axis, mode):
    """A generator that performs overlap-add circular convolution of a
    an array or producer of arrays with a 1-dimensional window.

    Args:
        pro:                        producer of ndarray(s)
        win (1-D array):            a 1-D window to convolve across arr
        axis (int):                 axis of arr or each producer array
                                    along which window should be convolved
        mode (str):                 one of 'full', 'same', 'valid'.
                                    identical to numpy convolve mode

    Numpy & Scipy implement oaconvolve but require that the input and  
    output be storable in RAM. This implementation makes no such constraint.
    The input iterable can be an in-memory array, a numpy memmap, a 
    generator of arrays or an iterable of arrays (see producer). 

    Returns: a generator of convolved arrays. The length of each array along
    axis will be (optimal_nffts(win) - len(win) - 1). See optimal_nffts
    func.
    """

    #fetch the producers initial chunksize
    isize = pro.chunksize

    #estimate optimal nfft and transform window
    nfft = optimal_nffts(win)
    M = len(win)
    H = fft.fft(win, nfft)

    #set the step size 'L' and compute num of segments
    L = nfft - M + 1
    nsegments = int(np.ceil(pro.shape[axis] / L))

    #initialize the overlap with shape  along axis
    overlap_shape = list(pro.shape)
    overlap_shape[axis] = M - 1
    overlap = np.zeros(overlap_shape)

    #set producer chunksize to L & perform overlap add
    pro.chunksize = L
    for segment_num, subarr in enumerate(pro):
        # pad the length L segment with M-1 zeros for overlap
        x = pad_along_axis(subarr, [0, M - 1], axis=axis)
        
        #perform circular convolution
        y = fft.ifft(fft.fft(x, nfft, axis=axis) * H, axis=axis).real
        
        #split filtered segment and overlap
        if segment_num < nsegments - 1:
            y, new_overlap = np.split(y, [L], axis=axis)
        
        else:
            # last sample for last segment is data + window size
            last = subarr.shape[axis] + M - 1
            y = slice_along_axis(y, 0, last, axis=axis)
        
        #add previous overlap to convolved and update overlap
        slices = [slice(None)] * subarr.ndim
        slices[axis] = slice(0, M - 1)
        y[tuple(slices)] += overlap
        overlap = new_overlap
        
        #apply the boundary mode to first and last segments
        if segment_num == 0 or segment_num == nsegments - 1:
            y = _oa_mode(y, segment_num, len(win), axis, mode)
        
        yield y

    #on iteration completion reset producer chunksize
    pro.chunksize = isize


# FIXME Improve docs around zi please see iir apply method
@as_producer  
def sosfilt(pro, sos, chunksize, axis, zi=None):
    """Batch applies a second-order-section fmt filter to a producer.

    Args:
        pro:                     producer of ndarrays
        sos (array):             a nsectios x 6 array of numerator &
                                 denominator coeffs from an iir filter
        chunksize (int):         amount of data along axis to filter per
                                 batch
        axis (int):              axis along which to apply the filter in
                                 chunksize batches
        zi (ndarray):            initial condition data (Default None ->
                                 zeros as intial condition)

    Returns: a generator of filtered values of len chunksize along axis
    """

    #set initial conditions for filter (see scipy sosfilt; zi)
    shape = list(pro.shape)
    shape[axis] = 2
    z = np.zeros((sos.shape[0], *shape)) if zi is None else zi
    #compute filter values & store current initial conditions 
    for idx, subarr in enumerate(pro):
        y, z = sps.sosfilt(sos, subarr, axis=axis, zi=z)
        yield y


@as_producer
def sosfiltfilt(pro, sos, chunksize, axis):
    """Batch applies a forward-backward filter in sos format to a producer
    of numpy arrays.

    Args:
        pro:                     producer of ndarrays
        sos (array):             a nsectios x 6 array of numerator &
                                 denominator coeffs from an iir filter
        chunksize (int):         amount of data along axis to filter per
                                 batch
        axis (int):              axis along which to apply the filter in
                                 chunksize batches

    Returns: a generator of forward-backward filtered values of len 
             chunksize along axis
    """

    #get initial value from producer
    subarr = next(iter(pro))
    x0 = slice_along_axis(subarr, 0, 1, axis=axis) 
    
    # build initial condition
    zi = sps.sosfilt_zi(sos) #nsections x 2
    s = [1] * len(pro.shape)
    s[axis] = 2
    zi = np.reshape(zi, (sos.shape[0], *s)) #nsections,1,2
    
    #create a producer of forward filter values
    forward = sosfilt(pro, sos, chunksize, axis, zi=zi*x0)
    
    #filter backwards each forward produced arr
    for idx, arr in enumerate(forward):
        flipped = np.flip(arr, axis=axis)
        #get the last value as initial condition for backward pass
        y0 = slice_along_axis(flipped, 0, 1, axis=axis)
        #filter in reverse, reflip and yield
        revfilt, z = sps.sosfilt(sos, flipped, axis=axis, zi=zi*y0)
        yield np.flip(revfilt, axis=axis)

# FIXME
# IMPLEMENT filtfilt for transfer function format filters
# IMPLEMENT lfilter for transfer function format filters

@as_producer
def polyphase_resample(pro, L, M, fs, chunksize, fir, axis, **kwargs):
    """Resamples an array or producer of arrays by a rational factor (L/M)
    using the polyphase decomposition.

    Args:
        pro: A producer of ndarrays
            The data producer to be resampled.
        L: int
            The expansion factor. L-1 interpolated values will be inserted
            between consecutive samples along axis.
        M: int
            The decimation factor describing which Mth samples of produced 
            data survive decimation. (E.g. M=10 -> every 10th survives)
        fs: int
            The sampling rate of produced data in Hz.
        chunksize: int
            The number of samples to hold in memory during upsampling.
            This method will require ~ 3 times chunksize in memory.
        fir: FIR filter
            An openseize fir filter class
        axis: int
            The axis of produced data along which downsampling will occur.
        kwargs:
            Any valid keyword for a Kaiser lowpass filter. The default 
            values for combined antialiasing & interpolation filter are:

                fstop: int
                    The stop band edge frequency. 
                    Defaults to fs // max(L,M).
                fpass: int
                    The pass band edge frequency. Must be less than fstop.
                    Defaults to fstop - fstop // 10.
                gpass: int
                    The pass band attenuation in dB. Defaults to a max loss
                    in the passband of 1 dB ~ 11% amplitude loss.
                gstop: int
                    The max attenuation in the stop band in dB. Defaults to
                    40 dB or 99%  amplitude attenuation.

    Returns: a producer of resampled data. The chunksize of the yielded
             arrays along axis will be the nearest multiple of M closest to 
             the supplied chunksize (e.g. if M=3 and chunksize=1000 the 
             yielded chunksize will be 1002 since 1002 % 3 == 0).
    """

    if M >= pro.shape[axis]:
        msg = 'Decimation factor must M={} be < pro.shape[{}] = {}'
        raise ValueError(msg.format(M, axis, pro.shape[axis]))

    # pathological case: pro has  < 3 chunks  -> autoreduce csize
    csize = chunksize
    if csize > pro.shape[axis] // 3:
        csize = pro.shape[axis]  // 3

    # kaiser antialiasing & interpolation filter coeffecients
    fstop = kwargs.pop('fstop', fs // max(L, M))
    fpass = kwargs.pop('fpass', fstop - fstop // 10)
    gpass, gstop = kwargs.pop('gpass', 1), kwargs.pop('gstop', 40)
    h = fir(fpass, fstop, fs, gpass, gstop).coeffs

    # ensure decimation of each produced array is integer samples
    if csize % M > 0:
        csize = int(np.ceil(csize / M) * M)

    # iterators for prior, current and next chunks of data
    x = producer(pro, csize, axis)
    y = producer(pro, csize, axis)
    z = producer(pro, csize, axis)
    iprior, icurrent, inext = (iter(pro) for pro in [x,y,z])

    # num pts to append left & right on axis to cover convolve overhang
    # must be divisible by M to ensure int slicing after resample.
    overhang = int(np.ceil((len(h) - 1) / M) * M)

    # initialize left/right pads for first data section
    left_shape = list(pro.shape)
    left_shape[axis] = overhang
    left = np.zeros(left_shape)
    # advance inext twice to get right pads
    next(inext)
    right = slice_along_axis(next(inext), 0,  overhang, axis=axis)

    # compute the first resampled chunk
    current = next(icurrent)
    padded = np.concatenate((left, current, right), axis=axis)
    resampled = sps.resample_poly(padded, up=L, down=M, axis=axis, window=h)

    # remove result points computed from pads
    a, b = int(overhang * L / M), -int(overhang * L / M)
    yield slice_along_axis(resampled, a, b, axis=axis)

    # resample remaining chunks
    cnt = z.shape[axis] // csize + bool(z.shape[axis] % csize) - 1
    for n, (last, curr, nxt) in enumerate(zip(iprior, icurrent, inext), 1):

        # build left and right pads for current
        left = slice_along_axis(last, -overhang, axis=axis)
        
        if n < cnt - 1:
            right = slice_along_axis(nxt, 0, overhang, axis=axis)
        else:
            # at cnt-1 chunks concantenate next to current
            curr = np.concatenate((curr, nxt), axis=axis) 
            right = np.zeros(left.shape)

        padded = np.concatenate((left, curr, right), axis=axis)
        resampled = sps.resample_poly(padded, L, M, axis=axis, window=h)
        yield slice_along_axis(resampled, a, b, axis=axis)


def modified_dft(arr, fs, nfft, window, axis, detrend, scaling):
    """Returns the windowed Discrete Fourier Transform of a real signal.

    Args:
        arr: ndarray
            An array of values to estimate the DFT along axis.
            This array is assumed to be real-valued (Hermetian symmetric)
        fs: int
            The sampling rate of the values in arr.
        nfft: int
            The number of frequencies between 0 and fs used to construct
            the DFT. If None, nfft will match the length of the arr. If 
            nfft is smaller than arr along axis, the array is cropped. If
            nfft is larger than arr along axis, the array is zero padded.
            The returned frequencies will be nfft//2 + 1 since this method
            returns only positive frequencies.
        window: str
            A scipy signal module window function. Please see references
            for all available windows.
        axis: int
            Axis along which the DFT will be computed.
        detrend: str
            The type of detrending to apply to data before computing
            DFT. Options are 'constant' and 'linear'. If constant, the
            mean of the data is removed before the DFT is computed. If
            linear, the linear trend in the data array along axis is 
            removed.
        scaling: str
            A string for determining the normalization of the DFT. If
            'spectrum', the DFT * np.conjugate(DFT) will have units V**2.
            If 'density' the DFT * np.conjugate(DFT) will have units 
            V**2 / Hz.

    Returns:
        A 1-D array of length nfft//2 + 1 of postive frequencies at which
        the DFT was computed.

        An ndarray of DFT the same shape as array except along axis which
        will have length nfft//2 + 1

    References:
        Shiavi, R. (2007). Introduction to Applied Statistical Signal 
        Analysis : Guide to Biomedical and Electrical Engineering 
        Applications. 3rd ed.

        Scipy windows:
        https://docs.scipy.org/doc/scipy/reference/signal.windows.html
    """

    nsamples = arr.shape[axis]

    if nfft < nsamples:
        # crop arr before detrending & windowing; see rfft crop
        arr = slice_along_axis(arr, 0, nfft, axis=-1)

    # detrend the array
    arr = sps.detrend(arr, axis=axis, type=detrend)

    # fetch and apply window
    coeffs = sps.get_window(window, arr.shape[axis])
    arr = arr * coeffs

    # compute real DFT. Zeropad for nfft > nsamples is automatic
    # rfft uses 'backward' norm default which is no norm on rfft
    arr = np.fft.rfft(arr, nfft, axis=axis)
    freqs = np.fft.rfftfreq(nfft, d=1/fs)

    # scale using weighted mean of window values
    if scaling == 'spectrum':
        norm = 1 / np.sum(coeffs)**2

    elif scaling == 'density':
        #process loss Shiavi Eqn 7.54
        norm = 1 / (fs * np.sum(coeffs**2))
    
    else:
        msg = 'Unknown scaling: {}'
        raise ValueError(msg.format(scaling))
   
    # before conjugate multiplication unlike scipy
    # see _spectral_helper lines 1808 an 1842.
    arr *= np.sqrt(norm)

    return freqs, arr


def periodogram(arr, fs, nfft=None, window='hann', axis=-1, 
                detrend='constant', scaling='density'):
    """Estimates the power spectrum of an ndarray using the windowed
    periodogram method.

    Args:
        arr: ndarray
            An array of values to estimate the PS or PSD along axis.
            This array is assumed to be real-valued (Hermetian symmetric)
        fs: int
            The sampling rate of the values in arr.
        nfft: int
            The number of frequencies between 0 and fs used to construct
            the Discrete Fourier Transform. If None, nfft will match the 
            length of the arr. If nfft is smaller than arr along axis, the
            array is cropped. If nfft is larger than arr along axis, the 
            array is zero padded. The returned frequencies will be 
            nfft//2 + 1 since this method returns only positive frequencies.
        window: str
            A scipy signal module window function. Please see references
            for all available windows.
        axis: int
            Axis along which the power spectrum will be estimated.
        detrend: str
            The type of detrending to apply to data before computing
            estimate. Options are 'constant' and 'linear'. If constant, the
            mean of the data is removed before the PS/PSD is estimated. If
            linear, the linear trend in the data array along axis is removed
            before the estimate.
        scaling: str
            A string for determining the normalization of the estimate. If
            'spectrum', the estimate will have units V**2 and be referred to
            as the power spectral estimate. If 'density' the estimate will
            have units V**2 / Hz and is referred to as the power spectral
            density estimate.
            
    Returns:
        A 1-D array of length NFFT/2 of postive frequencies at which the 
        estimate was computed.

        An ndarray of power spectral (density) estimates the same shape as
        array except along axis which we have length nfft//2 + 1.

    References:
        Schuster, Arthur (January 1898). "On the investigation of hidden 
        periodicities with application to a supposed 26 day period of 
        meteorological phenomena". Terrestrial Magnetism. 3 (1): 13–41

        Shiavi, R. (2007). Introduction to Applied Statistical Signal 
        Analysis : Guide to Biomedical and Electrical Engineering 
        Applications. 3rd ed.

        Scipy windows:
        https://docs.scipy.org/doc/scipy/reference/signal.windows.html
    """

    nfft = arr.shape[axis] if not nfft else int(nfft)
    
    # compute modified DFT & take modulus to get power spectrum
    freqs, arr = modified_dft(arr, fs, nfft, window, axis, detrend, scaling)
    arr = np.real(arr)**2 + np.imag(arr)**2 

    # since real FFT -> double for uncomputed negative freqs.
    slicer = [slice(None)] * arr.ndim
    if nfft % 2:
        # k=0 dft sample is not pos. or neg so not doubled
        slicer[axis] = slice(1, None)
    
    else:
        # last k=nfft/2 is not in the dft since nfft is odd
        slicer[axis] = slice(1, -1)
    
    arr[tuple(slicer)] *= 2

    return freqs, arr


def _spectra_estimatives(pro, fs, nfft, window, overlap, axis, detrend,
                         scaling, func, **kwargs):
    """Iteratively estimates the power spectrum or modified DFT for each
    nfft segment in a producer.

    This generator yields an estimate for each nfft segment. It should not
    be called externally.

    Args:
        func: function
            A function returning a spectral estimate for a window of data.
            E.g. periodogram, modified_dft
        kwargs: unused kwargs
            Future support for additional spectral estimate functions.
    """

    # num overlap points & shift between successive nfft segments
    noverlap = int(nfft * overlap)
    stride = nfft - noverlap

    # use FIFO to cache & release nfft & stride  num. samples respectively
    fifo = FIFOArray(chunksize=stride, axis=axis)
    for n, arr in enumerate(pro):

        # yield nfft sized estimates while fifo has >= nfft samples
        while fifo.qsize() >= nfft:
            
            # slice nfft samples to compute estimate
            x = slice_along_axis(fifo.queue, 0, nfft, axis=axis)
            f, y = func(x, fs, nfft, window, axis, detrend, scaling)
            
            # release stride samples leaving nover in FIFO
            fifo.get()
            yield y
        
        else:
            
            # provide fifo with more samples
            fifo.put(arr)
            continue
    
    else:
        
        # exhaust the fifo
        while fifo.qsize() >= nfft:
            
            x = slice_along_axis(fifo.queue, 0, nfft, axis=axis)
            f, y = func(x, fs, nfft, window, axis, detrend, scaling)
            fifo.get() 
            yield y


def welch(pro, fs, nfft, window, overlap, axis, detrend, scaling):
    """Iteratively estimates the power spectrum using Welch's method.

    Welch's method divides data into overlapping segments and computes the
    modified periodograms for each segment. Usually these segments are
    average over to estimate the PSD under the assumption that the data is
    stationary (i.e. the spectral content across segments is similar.)
    Unlike scipy, this method does not assert that the data is an in-memory
    array.

    Args:
        pro: A producer of ndarrays
            A data producer whose power spectral is to be estimated.
        fs: int
            The sampling rate of the produced data.
        nfft: int
            The number of frequencies in the interval [0, fs) to use to
            estimate the power spectra. This determines the frequency
            resolution of the estimate since resolution = fs / nfft.
        window: str
            A string name for a scipy window to be applied to each data
            segment before computing the periodogram of that segment. For
            a full list of windows see scipy.signal.windows.
        overlap: float
            A percentage in [0, 1) of the data segement that should overlap
            with the next data segment. If 0 this estimate is equivalent to
            Bartletts method (2)
        axis: int
            The sample axis of the producer. The estimate will be carried
            out along this axis.
        detrend: str either 'constant' or 'linear'
            A string indicating whether to subtract the mean ('constant') or
            subtract a linear fit ('linear') from each segment prior to
            computing the estimate for a segment.
        scaling: str either 'spectrum' or 'density'
            Determines the normalization to apply to the estimate. If
            'spectrum' the estimate will have units V**2 and if 'density'
            V**2 / Hz.

    Returns:
        A tuple containing a 1-D array of frequencies of lenght nfft//2 + 1
        and a producer of PS(D) estimates for each overlapped segment.

    Notes:
        Scipy allows for the segment length and number of DFT points (nfft)
        to be different. This allows for interpolation of frequencies. Given
        that EEG data typically has many samples, openseize locks the
        segment length to the nfft amount (i.e. no interpolation). Finer
        resolutions of the estimate will require longer data segements.

        Scipy welch drops the last segment of data if the number of points in 
        the segment is less than nfft. Openseize welch follows the same 
        convention. 

        Lastly, Openseize assumes the produced data is real-valued. This is
        appropriate for all EEG data. If you are calling this method on
        complex data, the imaginary part will be dropped.

    References:
        (1) P. Welch, "The use of the fast Fourier transform for the 
        estimation of power spectra: A method based on time averaging over 
        short, modified periodograms", IEEE Trans. Audio Electroacoust. vol.
        15, pp. 70-73, 1967

        (2) M.S. Bartlett, "Periodogram Analysis and Continuous Spectra", 
        Biometrika, vol. 37, pp. 1-16, 1950.

        (3) B. Porat, "A Course In Digitial Signal Processing" Chapters 4 &
        13. Wiley and Sons 1997.
    """

    # build the welch generating function
    genfunc = partial(_spectra_estimatives, pro, fs, nfft, window, overlap, 
                      axis, detrend, scaling, func=periodogram)

    # obtain the positive freqs.
    freqs = np.fft.rfftfreq(nfft, 1/fs)

    # num. segments that fit into pro samples of len nfft with % overlap
    nsegs = int((pro.shape[axis] - nfft) // (nfft * (1-overlap)) + 1)
    shape = list(pro.shape)
    shape[axis] = nsegs

    # return producer from welch gen func with each yielded 
    result = producer(genfunc, chunksize=len(freqs), axis=axis, shape=shape)
    return freqs, result


def stft(pro, fs, nfft, window, overlap, axis, detrend, scaling, boundary,
         padded):
    """Estimates the Discrete Short-time Fourier Transform of a real signal.

    STFT breaks the signal into overlapping segments and computes
    a windowed Discrete Fourier Transform for each segment. This is a
    complex sequence X(freq, time). The spectrogram is then 
    np.abs(X(freq, time))**2.

    Args:
        pro: A producer of ndarrays
            A data producer whose STFT is to be estimated.
        fs: int
            The sampling rate of the produced data.
        nfft: int
            The number of frequencies in the interval [0, fs) to use to
            estimate the spectra in each segment. This determines the 
            frequency resolution of the estimate since 
            resolution = fs / nfft.
        window: str
            A string name for a scipy window to be applied to each data
            segment before computing the periodogram of that segment. For
            a full list of windows see scipy.signal.windows.
        overlap: float
            A percentage in [0, 1) of the data segement that should overlap
            with the next data segment. In order for the STFT to be
            invertible. The overlap amount needs to equally weight all
            samples in pro. This is called the "constant overlap-add" COLA
            constraint. It is window dependent. If you intend to invert this
            STFT (synthesis) you  will need to verify this constraint is met
            using scipy.signal.check_COLA. 
        axis: int
            The sample axis of the producer. The estimate will be carried
            out along this axis.
        detrend: str either 'constant' or 'linear'
            A string indicating whether to subtract the mean ('constant') or
            subtract a linear fit ('linear') from each segment prior to
            computing the estimate for a segment.
        scaling: str either 'spectrum' or 'density'
            Determines the normalization to apply to the estimate for each
            segment. If 'spectrum', then np.abs(X)**2 is the magnitude 
            spectrum for each segment. If density np.abs(X)**2 is the
            density spectrum and may be integrated over to give the total 
            power for a segment.
        boundary: bool
            A boolean indicating if the first and last segments should be
            extended with zeros so that the first/last samples ar centered
            at nfft//2.  This allows for inversion of the first/last input 
            points for windows whose first & last values are 0. Unlike 
            scipy, openseize only allows for zero extensions since this has
            the clear interpretation of a zero-pad interpolation of the
            frequencies in the DFT.
        padded: bool
            Specifies if the last array of the producer should be padded
            with zeros so that a whole number of overlapped nfft segments
            fit into the signal length. This enables inversion of the stft
            since the entire signal is used. This is in contrast with Welch
            which drops the last segment if shorter than nfft - noverlap.

    Returns:
        f: 1-D array of frequencies
        t: 1-D array of segment times
        X: 2-D array of STFT estimates with the last axis corresponding to
           times (t).

    Notes:
        Scipy allows for the segment length and number of DFT points (nfft)
        to be different. This allows for interpolation of frequencies. Given
        that EEG data typically has many samples, openseize locks the
        segment length to the nfft amount (i.e. no interpolation). Finer
        resolutions of the estimate will require longer data segements.

        Openseize assumes the produced data is real-valued. This is
        appropriate for all EEG data. If you are calling this method on
        complex data, the imaginary part will be dropped.

    
    References:
        (1) Shiavi, R. (2007). Introduction to Applied Statistical Signal 
        Analysis : Guide to Biomedical and Electrical Engineering 
        Applications. 3rd ed.

        (2) B. Porat, "A Course In Digitial Signal Processing" Chapters 4 &
        13. Wiley and Sons 1997.
    """

    # num overlap points & shift between successive nfft segments
    noverlap = int(nfft * overlap)
    stride = nfft - noverlap

    # stft boundary & padding options
    data = pro
    if boundary:
        
        # center first & last segments by padding producer
        data = pad_producer(data, nfft//2, value=0)

    if padded:
        
        nsamples = pro.shape[axis]
        # pad w/ stride if samples not divisible by stride
        amt = stride if nsamples % stride else 0
        data = pad_producer(data, [0, amt], value=0)

    # build the stft generating function
    genfunc = partial(_spectra_estimatives, data, fs, nfft, window, overlap, 
                      axis, detrend, scaling, func=modified_dft)

    # obtain the positive freqs.
    freqs = np.fft.rfftfreq(nfft, 1/fs)
    
    # num. segments that fit into pro samples of len nfft with % overlap
    nsegs = int((data.shape[axis] - nfft) // (nfft * (1-overlap)) + 1)
    shape = list(data.shape)
    shape[axis] = nsegs

    # compute the segment times
    if boundary:
        time = 1 / fs * np.arange(0, data.shape[axis] + 1, nfft-noverlap)
    else:
        time = 1 / fs * np.arange(nfft//2, data.shape[axis] + 1 - nfft//2, 
                                  nfft-noverlap)

    # return producer from welch gen func with each yielded 
    result = producer(genfunc, chunksize=len(freqs), axis=axis, shape=shape)
    return freqs, time, result

