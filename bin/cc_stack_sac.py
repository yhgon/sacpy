#!/usr/bin/env python3
"""
Executable files for cross-correlation and stacking operations
"""
from genericpath import sameopenfile
from sacpy.processing import filter, taper, tukey_jit, temporal_normalize, frequency_whiten
from sacpy.sac import rd_sac_2, wrt_sac_2
from sacpy.sac import c_rd_sac, c_wrt_sac2
import time
import sacpy.geomath as geomath
from mpi4py import MPI
import h5py
import pyfftw
import numpy as np
import sys
import getopt
from glob import glob
from numba import jit

def main(   fnm_wildcard, tmark, t1, t2, delta, pre_detrend=True, pre_taper_ratio= 0.005, pre_filter= None,
            temporal_normalization = (128.0, 0.02, 0.066666), spectral_whiten= 0.02,
            dist_range = (0.0, 180.0), dist_step= 1.0, daz_range= None, gcd_ev_range= None, gc_center_rect= None,
            post_folding = True, post_taper_ratio = 0.005, post_filter = ('bandpass', 0.02, 0.066666), post_norm = True, post_cut= None,
            log_prefnm= 'cc_mpi_log',
            output_pre_fnm= 'junk', output_format= ['hdf5'] ):
    """
    The main function.

    The run consists of
    - 1. reading sac files and pre-processing;
    - 2. optional whitening;
    - 3. cross-correlation and stacking;
    - 4. post-processing of correlation stacks;
    - 5. output.

    The parameters for each step are:
    - 1. reading sac files and pre-processing:
        fnm_wildcard:    file name wildcards. (e.g., fnm_wildcard= download/event_*/processed/*.00.BHZ ) 
        tmark, t1, t2:   the cut window to read.
        delta:           sampling time interval of the sac time series.
        pre_detrend:     True or False
        pre_taper_ratio: a ratio value between 0 and 0.5
        pre_filter:      None to disable pre-filter or a tuple (btype, f1, f2) to enable the filter.
                         (e.g.,  pre_filter= ('lowpass', 1.0, 0.0), or pre_filter= ('bandpass', 0.02, 0.5) )
    - 2. optional whitening:
        temporal_normalization: temporal normalization parameter. (e.g., 128.0, 0.02, 0.066666)
                                set None to disable temporal normalization.
        spectral_whiten:        spectral whitening parameter in Hz. (e.g., spectral_whiten= 0.02)
                                set None to disable spectral whitening.
    - 3. cross-correlation and stacking:
        dist_range:   inter-distance range (e.g., dist_range=(0.0, 180.0) ).
        dist_step:    inter-distance step. The generated dist list will be `np.arange(dist_range[0], dist_range[1] + dist_step )`.
        daz_range:    selection criterion for daz range. Set `None` to disable. (e.g., daz_range=(-0.1, 15.0) )
        gcd_ev_range: selection criterion for the distance from the event to two receivers great-circle path.
                      Set `None` to disable. (e.g., gcd_ev_range= (-0.1, 20) )
        gc_center_rect: selection criterion for the center of the great-circle plane formed by the two receivers.
                        If the center is outside the area, then it is not used in the cross-correlation stacks.
                        Can be a list of rect (lo1, lo2, la1, la2).
                        E.g., gc_center_rect= [ (120, 180, 0, 35), (180, 190, 0, 10) ]
    - 4. post-processing of correlation stacks:
        post_folding:     True to enable folding the positive and the negative cross-correlation lags. False to disable.
        post_taper_ratio: taper ratio between 0 and 0.5 when enable post filter.
        post_filter:      None to disable post-filter or a tuple (btype, f1, f2) to enable the filter.
                          (e.g.,  post_filter= ('lowpass', 1.0, 0.0), or post_filter= ('bandpass', 0.02, 0.5) )
        post_norm:        Normalize each cross-correlation stack with the max positive amplitude.
    - 5. output:
        log_prefnm     : mpi log filename prefix;
        output_pre_fnm : output filename prefix;
        output_format  : a list of output format. (e.g., output_format=['sac'], output_format=['sac', 'h5'] )
    """
    ### Init MPI
    mpi_comm = MPI.COMM_WORLD.Dup()
    mpi_rank = mpi_comm.Get_rank()
    mpi_ncpu = mpi_comm.Get_size()
    mpi_log_fid = open('%s_%03d.txt' % (log_prefnm, mpi_rank), 'w' )

    mpi_print_log('Start! rank(%d) nproc(%d)' % (mpi_rank, mpi_ncpu), 0, mpi_log_fid, True)
    t_main_start = time.time()

    ### 1. read sac files and pre-processing
    # dependent parameters
    sampling_rate = 1.0/delta
    npts          = int( np.round((t2-t1)/delta) ) + 1
    fftsize       = npts * 2
    df            = 1.0/(delta*fftsize)
    taper_size    = int(npts*pre_taper_ratio)
    # logging
    if True: # user-defined parameters
        mpi_print_log('Set reading parameters', 0, mpi_log_fid, True)
        mpi_print_log('fnm_wildcard:    %s ' % (fnm_wildcard)          , 1, mpi_log_fid, True)
        mpi_print_log('tmark, t1, t2:   %s, %f, %f ' % (tmark, t1, t2) , 1, mpi_log_fid, True)
        mpi_print_log('delta:           %f ' % (delta)                 , 1, mpi_log_fid, True)
        mpi_print_log('pre_detrend:     %d ' % (pre_detrend)           , 1, mpi_log_fid, True)
        mpi_print_log('pre_taper_ratio: %f ' % (pre_taper_ratio)       , 1, mpi_log_fid, True)
        if pre_filter != None:
            mpi_print_log('pre_filter:      (%s, %f, %f)' % (pre_filter[0], pre_filter[1], pre_filter[2]), 1, mpi_log_fid, True)
        else:
            mpi_print_log('pre_filter:  None (disabled)', 1, mpi_log_fid, True)
        #
        mpi_print_log('', 1, mpi_log_fid, False)
        mpi_print_log('Dependent parameters:', 1, mpi_log_fid, False)
        mpi_print_log('sampling_rate: %f ' % (sampling_rate) , 1, mpi_log_fid, True)
        mpi_print_log('npts:          %d ' % (npts) , 1, mpi_log_fid, True)
        mpi_print_log('fftsize:       %d ' % (fftsize) , 1, mpi_log_fid, True)
        mpi_print_log('df:            %f ' % (df) , 1, mpi_log_fid, True)
        mpi_print_log('pre_taper_size:%d ' % (taper_size) , 1, mpi_log_fid, True)

    ### 2. optional whitening
    wt_size, wt_f1, wt_f2, wf_size = None, None, None, None
    if temporal_normalization != None:
        wt, wt_f1, wt_f2 = temporal_normalization
        wt_size = (int(np.round(wt / delta) ) //2 ) * 2 + 1
    if spectral_whiten != None:
        wf = spectral_whiten  # Hz
        wf_size = (int(np.round(wf / df) ) //2 ) * 2 + 1
    # acceleration for spectral whitening
    critical_parameter= 0.001
    w_speedup_i1, w_speedup_i2 = None, None
    if spectral_whiten != None:
        w_speedup_i1, w_speedup_i2 = get_bound(fftsize, sampling_rate, post_filter[1], post_filter[2]+spectral_whiten, critical_parameter)
    # logging
    if True: # user-defined parameters
        mpi_print_log('Set whitening parameters', 0, mpi_log_fid, True)
        if temporal_normalization != None:
            mpi_print_log('temporal normalization:   (%f sec, %f Hz, %f Hz), size= %d' % (temporal_normalization[0], 
                    temporal_normalization[1], temporal_normalization[2], wt_size), 1, mpi_log_fid, True)
        else:
            mpi_print_log('temporal normalization:   None (disabled)', 1, mpi_log_fid, True)
        if spectral_whiten != None:
            mpi_print_log('spetral whitening:        %f Hz, size=%d' % (spectral_whiten, wf_size) , 1, mpi_log_fid, True)
        else:
            mpi_print_log('spetral whitening:        None (disabled)', 1, mpi_log_fid, True)

    #### 3. selection criteria
    if daz_range != None or gcd_ev_range != None or gc_center_rect != None:
        if daz_range == None:
            daz_range = (-0.1, 90.1)
        if gcd_ev_range == None:
            gcd_ev_range = (-0.1, 90.1)
        if gc_center_rect == None:
            gc_center_rect = [(-9999, 9999, -9999, 9999)]
    # logging
    if True:
        if daz_range != None or gcd_ev_range != None or gc_center_rect != None:
            mpi_print_log('Set selection criteria', 0, mpi_log_fid, True)
            mpi_print_log('Selection of receiver pairs: daz(%.1f, %.1f) gcd_ev(%.1f, %.1f) ' % (daz_range[0], daz_range[1], gcd_ev_range[0], gcd_ev_range[1] ), 1, mpi_log_fid, True )
            mpi_print_log('Selection rect: ', 1, mpi_log_fid, True )
            for rect in gc_center_rect:
                mpi_print_log('(%.1f, %.1f, %.1f, %.1f)' % (rect[0], rect[1], rect[2], rect[3] ), 2, mpi_log_fid, True )
    ### 4. post-processing
    # dependent parameters
    critical_parameter= 0.001
    cc_index_range = get_bound(fftsize, sampling_rate, post_filter[1], post_filter[2], critical_parameter)
    # logging
    if True:
        mpi_print_log('Set post-processing parameters', 0, mpi_log_fid, True)
        mpi_print_log('post_folding:   %d' % post_folding, 1, mpi_log_fid, True)
        if post_filter != None:
            mpi_print_log('post_taper_ratio: %f' % (post_taper_ratio), 1, mpi_log_fid, True )
            mpi_print_log('post_filter:      (%s, %f, %f)' % (post_filter[0], post_filter[1], post_filter[2]), 1, mpi_log_fid, True)
        else:
            mpi_print_log('post_filter:  None (disabled)', 1, mpi_log_fid, True)
        mpi_print_log('post_norm:   %d' % (post_norm), 1, mpi_log_fid, True )
    if True:
        mpi_print_log('Enable accelerating cross-correlation', 0, mpi_log_fid, True)
        msg = 'Level parameter: %f (will abandon the spectra outside (%f,%f) Hz.)' % (critical_parameter, cc_index_range[0]*df, cc_index_range[1]*df )
        mpi_print_log(msg, 1, mpi_log_fid, True )

    ### 5. cross-correlation stack
    dist = np.arange(dist_range[0], dist_range[1] + dist_step )
    nrfft_valid = cc_index_range[1] # spectra bigger than this are not useful given the post-filter processing
    spec_stack_mat = np.zeros( (dist.size, nrfft_valid), dtype= np.complex64 )
    stack_count = np.zeros( dist.size, dtype=np.int32 )

    global_spec_stack_mat, global_stack_count, stack_mat= None, None, None
    if mpi_rank == 0:
        global_spec_stack_mat = np.zeros( (dist.size, nrfft_valid), dtype= np.complex64 )
        global_stack_count = np.zeros( dist.size, dtype=np.int32 )
        stack_mat = np.zeros((dist.size, fftsize-1), dtype=np.float32 )
    # logging
    if True: # user-defined parameters
        mpi_print_log('Set ccstack parameters', 0, mpi_log_fid, True)
        mpi_print_log('dist_range: [%f,%f]' % (dist_range[0], dist_range[1]), 1, mpi_log_fid, True)
        mpi_print_log('dist_step:   %f' % (dist_step), 1, mpi_log_fid, True)
        msg = '[%s,...,%s] (size=%d)' % (', '.join(['%.1f' % it for it in dist[:3] ] ),  
                                       ', '.join(['%.1f' % it for it in dist[-1:] ]), 
                                       len(dist) )
        mpi_print_log(msg, 1, mpi_log_fid, True)
    #######################################################################################################################################
    ### run
    #######################################################################################################################################
    directories   = sorted( glob('/'.join(fnm_wildcard.split('/')[:-1] ) ) )
    sac_wildcards = fnm_wildcard.split('/')[-1]
    njobs  = len(directories)
    nchunk = (njobs // mpi_ncpu) if (njobs % mpi_ncpu == 0) else (njobs // mpi_ncpu + 1)
    mpi_i1 = mpi_rank * nchunk
    mpi_i2 = mpi_i1 + nchunk
    # logging
    if True:
        mpi_print_log('Distribute MPI jobs...', 0, mpi_log_fid, True)
        mpi_print_log('total jobs: %d' % (njobs), 1, mpi_log_fid, True)
        mpi_print_log('job ranges on this node: [%d, %d)' % (mpi_i1, mpi_i2), 1, mpi_log_fid, True)
    ###
    if True:
        mpi_print_log('Running...', 0, mpi_log_fid, True)
    t_rd_whiten, t_ccstack = 0.0, 0.0
    for it in directories[mpi_i1: mpi_i2]:
        wildcards = '%s/%s' % (it, sac_wildcards)
        ### 1. read and pre-processing and whitening
        t_start = time.time()
        whitened_spectra_mat, stlo, stla, evlo, evla, az, baz = rd_preproc_whiten_single(wildcards, delta, tmark, t1, t2, npts,
                                                                                            pre_detrend, pre_taper_ratio, pre_filter,
                                                                                            fftsize, nrfft_valid, wt_size, wt_f1, wt_f2, wf_size, w_speedup_i1, w_speedup_i2, taper_size)
        #mpi_print_log('Read...', 1, mpi_log_fid, True)
        nsac = whitened_spectra_mat.shape[0]
        local_t_rd_whiten = time.time()-t_start

        ### 3.1 cc and stack
        #mpi_print_log('cc&stack...', 1, mpi_log_fid, True)
        t_start = time.time()
        if daz_range != None or gcd_ev_range != None or gc_center_rect != None:
            center_clo1 = np.array( [rect[0] for rect in gc_center_rect] )
            center_clo2 = np.array( [rect[1] for rect in gc_center_rect] )
            center_cla1 = np.array( [rect[2] for rect in gc_center_rect] )
            center_cla2 = np.array( [rect[3] for rect in gc_center_rect] )

            local_ncc = ccstack_selection_ev(whitened_spectra_mat, stack_count, stlo, stla, az, spec_stack_mat, evlo[0], evla[0],
                            daz_range[0], daz_range[1], gcd_ev_range[0], gcd_ev_range[1], dist_range[0], dist_range[1],
                            center_clo1, center_clo2, center_cla1, center_cla2,
                            dist_step, cc_index_range, dist_range[0] )
        else:
            local_ncc = ccstack(whitened_spectra_mat, stack_count, stlo, stla, spec_stack_mat, dist_step, cc_index_range, dist_range[0] )
        local_t_cc = time.time()-t_start

        ###
        t_rd_whiten = t_rd_whiten + local_t_rd_whiten
        t_ccstack = t_ccstack + local_t_cc
        if True:
            tmp = t_rd_whiten+ local_t_cc + 0.001
            msg = '%s. rd&whiten: %.1fs(%d%%)(%d)，ccstack %.1fs(%d%%) (%d)' % (wildcards,
                    t_rd_whiten, t_rd_whiten/tmp*100, nsac,
                    local_t_cc, local_t_cc/tmp*100, local_ncc )
            mpi_print_log(msg , 1, mpi_log_fid, True)
    if True:
        total_loop_time = t_rd_whiten + t_ccstack + 1.0e-3 # in case of zeros
        msg = 'Time consumption summation: (rd&whiten: %.1fs(%d%%)， ccstack %.1fs(%d%%))' % (
                        t_rd_whiten, t_rd_whiten/total_loop_time*100, 
                        t_ccstack, t_ccstack/total_loop_time*100 )
        mpi_print_log(msg, 1, mpi_log_fid, True)

    ### 3.1.2 MPI collecting
    if True:
        mpi_print_log('MPI collecting...', 0, mpi_log_fid, True)
    mpi_comm.Reduce([spec_stack_mat, MPI.C_FLOAT_COMPLEX], [global_spec_stack_mat, MPI.C_FLOAT_COMPLEX], MPI.SUM, root= 0)
    mpi_comm.Reduce([stack_count, MPI.INT32_T], [global_stack_count, MPI.INT32_T], MPI.SUM, root= 0 )
    mpi_comm.Barrier()

    ### 3.2 ifft
    if mpi_rank == 0:
        if True:
            mpi_print_log('Ifft', 0, mpi_log_fid, True)
        rollsize = npts-1
        for irow in range(dist.size):
            global_spec_stack_mat[irow][0] = 0.0
            x = pyfftw.interfaces.numpy_fft.irfft(global_spec_stack_mat[irow], fftsize)
            x = np.roll(x, rollsize)
            stack_mat[irow] = x[:-1]

    ### 4. post processing
    if mpi_rank == 0:
        if True:
            mpi_print_log('Post processing', 0, mpi_log_fid, True)
        cc_t1, cc_t2 = -rollsize*delta, rollsize*delta
        # 4.1 post folding
        if post_folding:
            for irow in range(dist.size):
                stack_mat[irow] += stack_mat[irow][::-1]
            stack_mat = stack_mat[:,rollsize:]
            cc_t1, cc_t2 = 0, rollsize*delta
        # 4.2 post filtering
        if post_filter:
            w = tukey_jit( stack_mat.shape[1], post_taper_ratio )
            btype, f1, f2 = post_filter
            for irow in range(dist.size):
                #stack_mat[irow] -= np.mean( stack_mat[irow] )
                #stack_mat[irow] = signal.detrend( stack_mat[irow] )
                stack_mat[irow] = filter(stack_mat[irow], sampling_rate, btype, (f1, f2), 2, 2 )
                stack_mat[irow] *= w
        # 4.3 post norm
        if post_norm:
            for irow in range(dist.size):
                v = np.max(stack_mat[irow])
                #print(irow, v)
                if v> 0.0:
                    stack_mat[irow] *= (1.0/v)
        # 4.4 post cut
        if post_cut != None:
            post_t1, post_t2 = post_cut
            if post_t1 < cc_t1:
                post_t1 = cc_t1
            if post_t2 > cc_t2:
                post_t2 = cc_t2
            post_i1 = int( round((post_t1-cc_t1)/delta) )
            post_i2 = int( round((post_t2-cc_t1)/delta) ) + 1
            cc_t1 = cc_t1 + post_i1 *delta
            cc_t2 = cc_t1 + (post_i2-post_i1-1)*delta
            stack_mat = stack_mat[:, post_i1:post_i2 ]
            if True:
                mpi_print_log('Post cutting...', 1, mpi_log_fid, True)
        if True:
            mpi_print_log('correlation time range (%f, %f)' % (cc_t1, cc_t2), 1, mpi_log_fid, True)

    ### 5. output
        if True:
            mpi_print_log('Output format(%s)' % (output_format) , 0, mpi_log_fid, True)
        if 'hdf5' in output_format or 'h5' in output_format:
            h5_fnm = '%s.h5' % (output_pre_fnm)
            if True:
                mpi_print_log(h5_fnm, 1, mpi_log_fid, True)
            f = h5py.File(h5_fnm, 'w' )
            dset = f.create_dataset('ccstack', data=stack_mat )
            dset.attrs['cc_t0'] = cc_t1
            dset.attrs['cc_t1'] = cc_t2
            dset.attrs['delta'] = delta
            f.create_dataset('stack_count', data= global_stack_count )
            f.create_dataset('dist', data= dist)
            f.close()
        if 'sac' in output_format or 'SAC' in output_format:
            if True:
                mpi_print_log('%s_..._sac' % (output_pre_fnm), 1, mpi_log_fid, True)
            for irow, it in enumerate(dist):
                fnm = '%s_%05.1f_.sac' % (output_pre_fnm, it)
                wrt_sac_2(fnm, stack_mat[irow], delta, cc_t1, user3= stack_count[irow] )

    ##### Done
    if True:
        mpi_print_log('Done (%.1f sec)' % (time.time()-t_main_start) , 0, mpi_log_fid, True)
    mpi_log_fid.close()

def mpi_print_log(msg, n_pre, file=sys.stdout, flush=False):
    if n_pre <= 0:
        print('>>> %s' % (msg), file= file, flush= flush )
    else:
        print('%s%s' % ('    '*n_pre, msg), file= file, flush= flush )

def rd_preproc_whiten_single(sacfnm_template, delta, tmark, t1, t2, npts,
                                pre_detrend, pre_taper_ratio , pre_filter, # preproc args
                                fftsize, nrfft_valid, wnd_size_t, w_f1, w_f2, wnd_size_freq, speedup_i1, speedup_i2, whiten_taper_ratio # whitening args
                            ):
    """
    Read, preproc, and whiten many sacfiles given a filename template `sacfnm_template`.
    Those sac files can be recorded at many receivers for the same event, or they
    can be recorded at the same receiver from different events.

    sacfnm_template: The filename template that contain wildcards. The function with use
                     `glob(sacfnm_template)` to obtain all sac filenames.
    tmark, t1, t2:   The cut time window to read sac time series.

    pre_detrend:     Set True to enable detrend after reading.
    pre_taper_ratio: Set taper ratio (0~-0.5) to apply taper after the optional detrend.
    pre_filter:      Filter all the time series. Set `None` to disable filter.
                     The filter can be ('bandpass', f1, f2), ('lowpass', fc, meaningless_f) or
                     ('highpass', fc, meaningless_f).

    Return: (spectra, stlo, stla, evlo, evla, az, baz)
    """
    ###
    fnms = sorted( glob(sacfnm_template) )
    nsac = len(fnms)
    ###
    sampling_rate = 1.0/delta
    btype, f1, f2 = pre_filter if pre_filter != None else (None, None, None)
    whiten_taper_length = int(npts * whiten_taper_ratio)
    spectra = np.zeros((nsac, nrfft_valid), dtype=np.complex64  )
    stlo = np.zeros(nsac, dtype=np.float32 )
    stla = np.zeros(nsac, dtype=np.float32 )
    evlo = np.zeros(nsac, dtype=np.float32 )
    evla = np.zeros(nsac, dtype=np.float32 )
    az   = np.zeros(nsac, dtype=np.float32 )
    baz  = np.zeros(nsac, dtype=np.float32 )
    ###
    index = 0
    for it in fnms:
        st = c_rd_sac(it, tmark, t1, t2, True, True)
        ## Check if Nan or all zeros.
        if (st is None) or (not np.any(st.dat)):
            continue
        ## preproc
        if pre_detrend:
            st.detrend()
        if pre_taper_ratio > 1.0e-5:
            st.taper(pre_taper_ratio)
        if pre_filter != None:
            st.dat = filter(st.dat, sampling_rate, btype, (f1, f2), 2, 2)
        ## whiten
        if wnd_size_t != None:
            st.dat = temporal_normalize(st.dat, sampling_rate, wnd_size_t, w_f1, w_f2, 1.0e-5, whiten_taper_length)
        if wnd_size_freq != None:
            st.dat = frequency_whiten(st.dat, wnd_size_freq, 1.0e-5, speedup_i1, speedup_i2, whiten_taper_length)
        ## fft and obtain valid values
        if np.all(np.isfinite(st.dat)) and np.any(st.dat):
            spectra[index] = (pyfftw.interfaces.numpy_fft.rfft(st.dat, fftsize)[:nrfft_valid] )
            hdr = st.hdr
            stlo[index] = hdr.stlo
            stla[index] = hdr.stla
            evlo[index] = hdr.evlo
            evla[index] = hdr.evla
            az[index]   = hdr.az
            baz[index]  = hdr.baz
            index = index + 1
    ###
    if index < nsac:
        spectra = spectra[:index, :]
        stlo = stlo[:index]
        stla = stla[:index]
        evlo = evlo[:index]
        evla = evla[:index]
        az   = az[:index]
        baz  = baz[:index]
    return spectra, stlo, stla, evlo, evla, az, baz

def get_bound(fftsize, sampling_rate, f1, f2, critical_level= 0.01):
    x = np.zeros(fftsize)
    x[0] = 1.0
    x = filter(x, sampling_rate, 'bandpass', (f1, f2), 2, 2)
    s = pyfftw.interfaces.numpy_fft.rfft(x, fftsize)
    amp = np.abs(s)
    c = np.max(amp) * critical_level
    i1 = np.argmax(amp>c)
    i2 = np.argmin(amp[i1:]>c) + i1 + 1
    if i1 < 0:
        i1 = 0
    if i2 >= fftsize:
        i2 = fftsize
    return i1, i2

@jit(nopython=True, nogil=True)
def ccstack(spec_mat, stack_count, stlo_lst, stla_lst, stack_mat, dist_step, index_range, dist_start=0.0):
    """
    """
    count = 0
    i1, i2 = index_range
    nsac = spec_mat.shape[0]
    for isac1 in range(nsac):
        stlo1, stla1 = stlo_lst[isac1], stla_lst[isac1]
        spec1 = spec_mat[isac1]
        for isac2 in range(isac1, nsac):
            stlo2, stla2 = stlo_lst[isac2], stla_lst[isac2]
            spec2 = spec_mat[isac2]
            ###
            dist = geomath.haversine(stlo1, stla1, stlo2, stla2)
            idx = int( np.round((dist-dist_start) / dist_step) )
            w   = np.conj(spec1[i1:i2]) * spec2[i1:i2]
            stack_mat[idx][i1:i2] += w
            stack_count[idx] += 1
            count += 1
    return count

@jit(nopython=True, nogil=True)
def ccstack_selection_ev(spec_mat, stack_count, stlo_lst, stla_lst, az_lst, stack_mat,
                        evlo, evla, daz_min, daz_max, gcd_ev_min, gcd_ev_max, dist_min, dist_max,
                        center_clo1, center_clo2, center_cla1, center_cla2,
                        dist_step, index_range, dist_start=0.0):
    """
    """
    daz1, daz2 = daz_min, daz_max
    gcd1, gcd2 = gcd_ev_min, gcd_ev_max
    dist1, dist2 = dist_min-dist_step*0.5, dist_max+dist_step*0.5
    count = 0
    i1, i2 = index_range
    nsac = spec_mat.shape[0]
    for isac1 in range(nsac):
        stlo1, stla1, az1 = stlo_lst[isac1], stla_lst[isac1], az_lst[isac1]
        spec1 = spec_mat[isac1]
        for isac2 in range(isac1, nsac):
            stlo2, stla2, az2 = stlo_lst[isac2], stla_lst[isac2], az_lst[isac2]
            ### dist selection
            dist = geomath.haversine(stlo1, stla1, stlo2, stla2)
            if dist < dist1 or dist > dist2:
                continue
            ### rect selection
            flag = 0
            if isac1==isac2 or (abs(stlo1-stlo2)<1.0e-3 and abs(stla1-stla2)<1.0e-3 ):
                (x, y), (x1, y1) = geomath.great_circle_plane_center(evlo, evla, stlo1, stla1)
                if y<0:
                    x, y = x1, y1
                for lo1, lo2, la1, la2 in zip(center_clo1, center_clo2, center_cla1, center_cla2):
                    if lo1 < lo2:
                        if x<lo1 or x>lo2 or y<la1 or y >la2:
                            flag = 1
                            continue
                    else:
                        if x>lo1 or x<lo2 or y<la1 or y >la2:
                            flag = 1
                            continue
            else:
                (x, y), (x1, y1) = geomath.great_circle_plane_center(stlo1, stla1, stlo2, stla2)
                if y<0:
                    x, y = x1, y1
                for lo1, lo2, la1, la2 in zip(center_clo1, center_clo2, center_cla1, center_cla2):
                    if lo1 < lo2:
                        if x<lo1 or x>lo2 or y<la1 or y >la2:
                            flag = 1
                            continue
                    else:
                        if x>lo1 or x<lo2 or y<la1 or y >la2:
                            flag = 1
                            continue
            if flag == 1:
                continue
            ### daz selection
            daz = (az1-az2) % 360.0
            if daz > 180.0:
                daz = 360.0-daz
            if daz > 90.0:
                daz = 90.0-daz
            if daz < daz1 or daz > daz2:
                continue
            ### gcd selection
            gcd = abs( geomath.point_distance_to_great_circle_plane(evlo, evla, stlo1, stla1, stlo2, stla2) )
            if gcd < gcd1 or gcd > gcd2:
                continue

            ###
            spec2 = spec_mat[isac2]
            ###
            idx = int( np.round((dist-dist_start) / dist_step) )
            w   = np.conj(spec1[i1:i2]) * spec2[i1:i2]
            stack_mat[idx][i1:i2] += w
            stack_count[idx] += 1
            count += 1
    return count

if __name__ == "__main__":
    fnm_wildcard = ''
    tmark, t1, t2 = -5, 10800, 32400
    delta = 0.1
    pre_detrend=True
    pre_taper_ratio= 0.005
    pre_filter= None

    temporal_normalization = None #(128.0, 0.02, 0.066666)
    spectral_whiten= None #0.02

    dist_range = (0.0, 180.0)
    dist_step= 1.0
    daz_range = None
    gcd_ev_range = None
    gc_center_rect = None

    post_folding = False
    post_taper_ratio = 0.005
    post_filter = ('bandpass', 0.02, 0.066666)
    post_norm = False
    post_cut = None

    log_prefnm= 'cc_mpi_log'
    output_pre_fnm= 'junk'
    output_format='hdf5'

    ######################
    HMSG = """\n
    %s  -I "in*/*.sac" -T 0/10800/32400 -D 0.1 -O cc_stack --out_format hdf5
        [--pre_detrend] [--pre_taper 0.005] [--pre_filter bandpass/0.005/0.1] 
        --stack_dist 0/180/1 [--daz -0.1/15] [--gcd_ev -0.1/20] [--gc_center_rect 120/180/0/40,180/190/0/10]
        [--w_temporal 128.0/0.02/0.06667] [--w_spec 0.02] 
        [--post_fold] [--post_taper 0.05] [--post_filter bandpass/0.02/0.0666] [--post_norm] [--post_cut]
         --log cc_log

Args:
    #1. I/O and pre-processing args:
    -I  : filename wildcards.
    -T  : cut time window for reading.
    -D  : delta.
    -O  : output filename prefix.
    --out_format:  output formate (can be 'hdf5' or 'sac', or 'hdf5,sac' ).
    --pre_detrend: use this to enable detrend.
    --pre_taper 0.005 : set taper ratio (default in 0.005)
    [--pre_filter bandpass/0.005/0.1]: set pre filter.

    #2. whitening parameter.
    [--w_temporal] : temporal normalization. (default is disabled)
    [--w_spec] : spectral whitening. (default is disabled)

    #3. stacking method
    --stack_dist :
    [--daz]      :
    [--gcd_ev]   :
    [--gc_center_rect] : a list of rect (lo1, lo2, la1, la2) to exclude some receiver pairs.

    #4. post-processing parameters:
    [--post_fold]
    [--post_taper]
    [--post_filter]
    [--post_norm]
    [--post_cut]

    #5. log:
    --log : log filename prefix.

E.g.,
    %s -I "in*/*.sac" -T 0/10800/32400 -D 0.1 -O cc --out_format hdf5,sac
        --pre_detrend --pre_taper 0.005 
        --w_temporal 128.0/0.02/0.06667 --w_spec 0.02
        --stack_dist 0/180/1
        --daz -0.1/20 --gcd_ev -0.1/30 --gc_center_rect 120/180/0/40
        --post_fold --post_taper 0.005 --post_filter bandpass/0.001/0.06667 --post_norm  --post_cut 0/5000
        --log cc_log  

    """

    if len(sys.argv) <= 1:
        print(HMSG % (sys.argv[0], sys.argv[0]), flush=True)
        sys.exit(0)
    ######################
    ######################
    options, remainder = getopt.getopt(sys.argv[1:], 'I:T:D:O:',
                            ['pre_detrend', 'pre_taper=', 'pre_filter=',
                             'w_temporal=', 'w_spec=',
                             'stack_dist=', 'daz=', 'gcd_ev=', 'gc_center_rect=',
                             'post_fold', 'post_taper=', 'post_filter=', 'post_norm', 'post_cut=',
                             'log=', 'out_format='] )
    for opt, arg in options:
        if opt in ('-I'):
            fnm_wildcard = arg
        elif opt in ('-T'):
            tmark, t1, t2 = arg.split('/')
            tmark = int(tmark)
            t1 = float(t1)
            t2 = float(t2)
        elif opt in ('--pre_detrend'):
            pre_detrend = True
        elif opt in ('--pre_taper'):
            pre_taper_ratio = float(arg)
        elif opt in ('--pre_filter'):
            junk1, junk2, junk3 = arg.split('/')
            pre_filter = (junk1, float(junk2), float(junk3) )
        elif opt in ('--w_temporal'):
            temporal_normalization = [float(it) for it in arg.split('/') ]
        elif opt in ('--w_spec'):
            spectral_whiten = float(arg)
        elif opt in ('--stack_dist'):
            x, y, z = [float(it) for it in arg.split('/') ]
            dist_range, dist_step = (x, y), z
        elif opt in ('--daz'):
            daz_range = [float(it) for it in arg.split('/') ]
        elif opt in ('--gcd_ev'):
            gcd_ev_range = [float(it) for it in arg.split('/') ]
        elif opt in ('--gc_center_rect'):
            gc_center_rect = []
            for rect in arg.split(','):
                tmp = [float(it) for it in rect.split('/') ]
                tmp[0] = tmp[0] % 360.0
                tmp[1] = tmp[1] % 360.0
                gc_center_rect.append( tmp )
        elif opt in ('--post_fold'):
            post_folding = True
        elif opt in ('--post_taper'):
            post_taper_ratio = float(arg)
        elif opt in ('--post_filter'):
            junk1, junk2, junk3 = arg.split('/')
            post_filter = (junk1, float(junk2), float(junk3) )
        elif opt in ('--post_norm'):
            post_norm = True
        elif opt in ('--post_cut'):
            post_cut = tuple( [float(it) for it in arg.split('/') ] )
        elif opt in ('--log'):
            log_prefnm = arg
        elif opt in ('-O'):
            output_pre_fnm = arg
        elif opt in ('--out_format'):
            output_format = arg.split(',')
    #######
    main(fnm_wildcard, tmark, t1, t2, delta, pre_detrend, pre_taper_ratio, pre_filter,
            temporal_normalization, spectral_whiten,
            dist_range, dist_step, daz_range, gcd_ev_range, gc_center_rect,
            post_folding, post_taper_ratio, post_filter, post_norm, post_cut,
            log_prefnm, output_pre_fnm, output_format )
    ########


