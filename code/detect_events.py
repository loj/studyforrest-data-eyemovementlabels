#!/usr/bin/python
# -*- coding: iso-8859-15 -*-
import numpy as np
from statsmodels.robust.scale import mad
from scipy import signal
from scipy import ndimage
from scipy.signal import savgol_filter # Savitzky–Golay filter, for smoothing data
from scipy.ndimage import median_filter
import sys
import gzip
from math import (
    degrees,
    atan2,
)

import logging
lgr = logging.getLogger('studyforrest.detect_eyegaze_events')


def deg_per_pixel(screen_size, viewing_distance, screen_resolution):
    """Determine `px2deg` factor for EyegazeClassifier

    Parameters
    ----------
    screen_size : float
      Either vertical or horizontal dimension of the screen in any unit.
    viewing_distance : float
      Viewing distance from the screen in the same unit as `screen_size`.
    screen_resolution : int
      Number of pixels along the dimensions reported for `screen_size`.
    """
    return degrees(atan2(.5 * screen_size, viewing_distance)) / \
        (.5 * screen_resolution)


def find_peaks(vels, threshold):
    """Find above-threshold time periods

    Parameters
    ----------
    vels : array
      Velocities.
    threshold : float
      Velocity threshold.

    Returns
    -------
    list
      Each item is a tuple with start and end index of the window where
      velocities exceed the threshold.
    """
    def _get_vels(start, end):
        v = vels[start:end]
        v = v[~np.isnan(v)]
        return v

    sacs = []
    sac_on = None
    for i, v in enumerate(vels):
        if sac_on is None and v > threshold:
            # start of a saccade
            sac_on = i
        elif sac_on is not None and v < threshold:
            sacs.append([sac_on, i, _get_vels(sac_on, i)])
            sac_on = None
    if sac_on:
        # end of data, but velocities still high
        sacs.append([sac_on, len(vels) - 1, _get_vels(sac_on, len(vels) - 1)])
    return sacs


def find_saccade_onsetidx(vels, start_idx, sac_onset_velthresh):
    idx = start_idx
    while idx > 0 \
            and (vels[idx] > sac_onset_velthresh or
                 vels[idx] <= vels[idx - 1]):
        # find first local minimum after vel drops below onset threshold
        # going backwards in time

        # we used to do this, but it could mean detecting very long
        # saccades that consist of (mostly) missing data
        #         or np.isnan(vels[sacc_start])):
        idx -= 1
    return idx


def find_movement_offsetidx(vels, start_idx, off_velthresh):
    idx = start_idx
    # shift saccade end index to the first element that is below the
    # velocity threshold
    while idx < len(vels) - 1 \
            and (vels[idx] > off_velthresh or
                 (vels[idx] > vels[idx + 1])):
            # we used to do this, but it could mean detecting very long
            # saccades that consist of (mostly) missing data
            #    or np.isnan(vels[idx])):
        idx += 1
    return idx


def find_psoend(velocities, sac_velthresh, sac_peak_velthresh):
        pso_peaks = find_peaks(velocities, sac_peak_velthresh)
        if pso_peaks:
            pso_label = 'HPSO'
        else:
            pso_peaks = find_peaks(velocities, sac_velthresh)
            if pso_peaks:
                pso_label = 'LPSO'
        if not pso_peaks:
            # no PSO
            return

        # find minimum after the offset of the last reported peak
        pso_end = find_movement_offsetidx(
            velocities, pso_peaks[-1][1], sac_velthresh)

        if pso_end > len(velocities):
            # velocities did not go down within the given window
            return

        return pso_label, pso_end


def filter_spikes(data):
    """In-place high-frequency spike filter

    Inspired by:

      Stampe, D. M. (1993). Heuristic filtering and reliable calibration
      methods for video-based pupil-tracking systems. Behavior Research
      Methods, Instruments, & Computers, 25(2), 137–142.
      doi:10.3758/bf03204486
    """
    def _filter(arr):
        # over all triples of neighboring samples
        for i in range(1, len(arr) - 1):
            if (arr[i - 1] < arr[i] and arr[i] > arr[i + 1]) \
                    or (arr[i - 1] > arr[i] and arr[i] < arr[i + 1]):
                # immediate sign-reversal of the difference from
                # x-1 -> x -> x+1
                prev_dist = abs(arr[i - 1] - arr[i])
                next_dist = abs(arr[i + 1] - arr[i])
                # replace x by the neighboring value that is closest
                # in value
                arr[i] = arr[i - 1] \
                    if prev_dist < next_dist else arr[i + 1]
        return arr

    data['x'] = _filter(data['x'])
    data['y'] = _filter(data['y'])
    return data


def get_dilated_nan_mask(arr, iterations, max_ignore_size=None):
    clusters, nclusters = ndimage.label(np.isnan(arr))
    # go through all clusters and remove any cluster that is less
    # the max_ignore_size
    for i in range(nclusters):
        # cluster index is base1
        i = i + 1
        if (clusters == i).sum() <= max_ignore_size:
            clusters[clusters == i] = 0
    # mask to cover all samples with dataloss > `max_ignore_size`
    mask = ndimage.binary_dilation(clusters > 0, iterations=iterations)
    return mask



class EyegazeClassifier(object):

    record_field_names = [
        'id', 'label',
        'start_time', 'end_time',
        'start_x', 'start_y',
        'end_x', 'end_y',
        'amp', 'peak_vel', 'avg_vel',
    ]

    def __init__(self,
                 px2deg,
                 sampling_rate,
                 velthresh_startvelocity=300.0,
                 min_intersaccade_duration=0.04,
                 min_saccade_duration=0.01,
                 max_initial_saccade_freq=2.0,
                 saccade_context_window_length=1.0,
                 max_pso_duration=0.04,
                 min_fixation_duration=0.04,
                 max_fixation_amp=0.7):
            self.px2deg = px2deg
            self.sr = sr = sampling_rate
            self.velthresh_startvel = velthresh_startvelocity
            self.max_fix_amp = max_fixation_amp

            # convert to #samples
            self.min_intersac_dur = int(
                min_intersaccade_duration * sr)
            self.min_sac_dur = int(
                min_saccade_duration * sr)
            self.sac_context_winlen = int(
                saccade_context_window_length * sr)
            self.max_pso_dur = int(
                max_pso_duration * sr)
            self.min_fix_dur = int(
                min_fixation_duration * sr)

            self.max_sac_freq = max_initial_saccade_freq / sr

    # TODO dissolve
    def _get_signal_props(self, data):
        data = data[~np.isnan(data['vel'])]
        pv = data['vel'].max()
        amp = (((data[0]['x'] - data[-1]['x']) ** 2 + \
                (data[0]['y'] - data[-1]['y']) ** 2) ** 0.5) * self.px2deg
        medvel = np.median(data['vel'])
        return amp, pv, medvel

    def get_adaptive_saccade_velocity_velthresh(self, vels):
        """Determine saccade peak velocity threshold.

        Takes global noise-level of data into account. Implementation
        based on algorithm proposed by NYSTROM and HOLMQVIST (2010).

        Parameters
        ----------
        start : float
          Start velocity for adaptation algorithm. Should be larger than
          any conceivable minimal saccade velocity (in deg/s).
        TODO std unit multipliers

        Returns
        -------
        tuple
          (peak saccade velocity threshold, saccade onset velocity threshold).
          The latter (and lower) value can be used to determine a more precise
          saccade onset.
        """
        cur_thresh = self.velthresh_startvel

        def _get_thresh(cut):
            # helper function
            vel_uthr = vels[vels < cut]
            med = np.median(vel_uthr)
            scale = mad(vel_uthr)
            return med + 10 * scale, med, scale

        # re-compute threshold until value converges
        count = 0
        dif = 2
        while dif > 1 and count < 30:  # less than 1deg/s difference
            old_thresh = cur_thresh
            cur_thresh, med, scale = _get_thresh(old_thresh)
            if not cur_thresh:
                # safe-guard in case threshold runs to zero in
                # case of really clean and sparse data
                cur_thresh = old_thresh
                break
            lgr.debug(
                'Saccade threshold velocity: %.1f '
                '(non-saccade mvel: %.1f, stdvel: %.1f)',
                cur_thresh, med, scale)
            dif = abs(old_thresh - cur_thresh)
            count += 1

        return cur_thresh, (med + 5 * scale)

    def _mk_event_record(self, data, idx, label, start, end):
        return dict(zip(self.record_field_names, (
            idx,
            label,
            start,
            end,
            data[start]['x'],
            data[start]['y'],
            data[end - 1]['x'],
            data[end - 1]['y']) +
            self._get_signal_props(data[start:end])))

    def __call__(self, data, classify_isp=True, sort_events=True):
        # find threshold velocities
        sac_peak_med_velthresh, sac_onset_med_velthresh = \
            self.get_adaptive_saccade_velocity_velthresh(data['med_vel'])
        lgr.info(
            'Global saccade MEDIAN velocity thresholds: '
            '%.1f, %.1f (onset, peak)',
            sac_onset_med_velthresh, sac_peak_med_velthresh)

        saccade_locs = find_peaks(
            data['med_vel'],
            sac_peak_med_velthresh)

        events = []
        saccade_events = []
        for e in self._detect_saccades(
                saccade_locs,
                data,
                0,
                len(data),
                context=self.sac_context_winlen):
            saccade_events.append(e.copy())
            events.append(e)

        lgr.info('Start ISP classification')

        if classify_isp:
            events.extend(self._classify_intersaccade_periods(
                data,
                0,
                len(data),
                # needs to be in order of appearance
                sorted(saccade_events, key=lambda x: x['start_time']),
                saccade_detection=True))

        # make timing info absolute times, not samples
        for e in events:
            for i in ('start_time', 'end_time'):
                e[i] = e[i] / self.sr

        return sorted(events, key=lambda x: x['start_time']) \
            if sort_events else events

    def _detect_saccades(
            self,
            candidate_locs,
            data,
            start,
            end,
            context):

        saccade_events = []

        if context is None:
            # no context size was given, use all data
            # to determine velocity thresholds
            lgr.debug(
                'Determine velocity thresholds on full segment '
                '[%i, %i]', start, end)
            sac_peak_velthresh, sac_onset_velthresh = \
                self.get_adaptive_saccade_velocity_velthresh(
                    data['vel'][start:end])
            if candidate_locs is None:
                lgr.debug(
                    'Find velocity peaks on full segment '
                    '[%i, %i]', start, end)
                candidate_locs = [
                    (e[0] + start, e[1] + start, e[2]) for e in find_peaks(
                        data['vel'][start:end],
                        sac_peak_velthresh)]

        # status map indicating which event class any timepoint has been
        # assigned to so far
        status = np.zeros((len(data),), dtype=int)

        # loop over all peaks sorted by the sum of their velocities
        # i.e. longer and faster goes first
        for i, props in enumerate(sorted(
                candidate_locs, key=lambda x: x[2].sum(), reverse=True)):
            sacc_start, sacc_end, peakvels = props
            lgr.info(
                'Process peak velocity window [%i, %i] at ~%.1f deg/s',
                sacc_start, sacc_end, peakvels.mean())

            if context:
                # extract velocity data in the vicinity of the peak to
                # calibrate threshold
                win_start = max(
                    start,
                    sacc_start - int(context / 2))
                win_end = min(
                    end,
                    sacc_end + context - (sacc_start - win_start))
                lgr.debug(
                    'Determine velocity thresholds in context window '
                    '[%i, %i]', win_start, win_end)
                lgr.debug('Actual context window: [%i, %i] -> %i',
                          win_start, win_end, win_end - win_start)

                sac_peak_velthresh, sac_onset_velthresh = \
                    self.get_adaptive_saccade_velocity_velthresh(
                        data['vel'][win_start:win_end])

            lgr.info('Active saccade velocity thresholds: '
                     '%.1f, %.1f (onset, peak)',
                     sac_onset_velthresh, sac_peak_velthresh)

            # move backwards in time to find the saccade onset
            sacc_start = find_saccade_onsetidx(
                data['vel'], sacc_start, sac_onset_velthresh)

            # move forward in time to find the saccade offset
            sacc_end = find_movement_offsetidx(
                data['vel'], sacc_end, sac_onset_velthresh)

            sacc_data = data[sacc_start:sacc_end]
            if sacc_end - sacc_start < self.min_sac_dur:
                lgr.debug('Skip saccade candidate, too short')
                continue
            elif np.sum(np.isnan(sacc_data['x'])):  # pragma: no cover
                # should not happen
                lgr.debug('Skip saccade candidate, missing data')
                continue
            elif status[
                    max(0,
                        sacc_start - self.min_intersac_dur):min(
                    len(data), sacc_end + self.min_intersac_dur)].sum():
                lgr.debug('Skip saccade candidate, too close to another event')
                continue

            lgr.debug('Found SACCADE [%i, %i]',
                      sacc_start, sacc_end)
            event = self._mk_event_record(data, i, "SACC", sacc_start, sacc_end)

            yield event.copy()
            saccade_events.append(event)

            # mark as a saccade
            status[sacc_start:sacc_end] = 1

            pso = find_psoend(
                data['vel'][sacc_end:sacc_end + self.max_pso_dur],
                sac_onset_velthresh,
                sac_peak_velthresh)
            if pso:
                pso_label, pso_end = pso
                lgr.debug('Found %s [%i, %i]',
                          pso_label, sacc_end, pso_end)
                psoevent = self._mk_event_record(
                    data, i, pso_label, sacc_end, sacc_end + pso_end)
                if psoevent['amp'] < saccade_events[-1]['amp']:
                    # discard PSO with amplitudes larger than their
                    # anchor saccades
                    yield psoevent.copy()
                    # mark as a saccade part
                    status[sacc_end:sacc_end + pso_end] = 1
                else:
                    lgr.debug(
                        'Ignore PSO, amplitude large than that of '
                        'the previous saccade: %.1f >= %.1f',
                        psoevent['amp'], saccade_events[-1]['amp'])

            if self.max_sac_freq and \
                    float(len(saccade_events)) / len(data) > self.max_sac_freq:
                lgr.info('Stop initial saccade detection, max frequency '
                         'reached')
                break

    def _classify_intersaccade_periods(
            self,
            data,
            start,
            end,
            saccade_events,
            saccade_detection):

        lgr.warn(
            'Determine ISPs %i, %i (%i saccade-related events)',
            start, end, len(saccade_events))

        prev_sacc = None
        prev_pso = None
        for ev in saccade_events:
            if prev_sacc is None:
                if 'SAC' not in ev['label']:
                    continue
            elif prev_pso is None and 'PS' in ev['label']:
                prev_pso = ev
                continue
            elif 'SAC' not in ev['label']:
                continue

            # at this point we have a previous saccade (and possibly its PSO)
            # on record, and we have just found the next saccade
            # -> inter-saccade window is determined
            if prev_sacc is None:
                win_start = start
            else:
                if prev_pso is not None:
                    win_start = prev_pso['end_time']
                else:
                    win_start = prev_sacc['end_time']
            # enforce dtype for indexing
            win_end = ev['start_time']
            if win_start == win_end:
                prev_sacc = ev
                prev_pso = None
                continue

            lgr.warn('Found ISP [%i:%i]', win_start, win_end)
            for e in self._classify_intersaccade_period(
                    data,
                    win_start,
                    win_end,
                    saccade_detection=saccade_detection):
                yield e

            # lastly, the current saccade becomes the previous one
            prev_sacc = ev
            prev_pso = None

        if prev_sacc is not None and prev_sacc['end_time'] == end:
            return

        lgr.debug("LAST_SEGMENT_ISP: %s -> %s", prev_sacc, prev_pso)
        # and for everything beyond the last saccade (if there was any)
        for e in self._classify_intersaccade_period(
                data,
                start if prev_sacc is None
                else prev_sacc['end_time'] if prev_pso is None
                else prev_pso['end_time'],
                end,
                saccade_detection=saccade_detection):
            yield e

    def _classify_intersaccade_period(
            self,
            data,
            start,
            end,
            saccade_detection):
        lgr.warn('Determine NaN-free intervals in [%i:%i] (%i)',
                 start, end, end - start)

        # split the ISP up into its non-NaN pieces:
        win_start = None
        for idx in range(start, end + 1):
            if win_start is None and not np.isnan(data['x'][idx]):
                win_start = idx
            elif win_start is not None and \
                    ((idx == end) or np.isnan(data['x'][idx])):
                for e in self._classify_intersaccade_period_helper(
                        data,
                        win_start,
                        idx,
                        saccade_detection):
                    yield e
                # reset non-NaN window start
                win_start = None

    def _classify_intersaccade_period_helper(
            self,
            data,
            start,
            end,
            saccade_detection):
        # no NaN values in data at this point!
        lgr.warn(
            'Process non-NaN segment [%i, %i] -> %i',
            start, end, end - start)

        label_remap = {
            'SACC': 'ISAC',
            'HPSO': 'IHPS',
            'LPSO': 'ILPS',
        }

        length = end - start
        # detect saccades, if the there is enough space to maintain minimal
        # distance to other saccades
        if length > (
                2 * self.min_intersac_dur) \
                + self.min_sac_dur + self.max_pso_dur:
            lgr.warn('Perform saccade detection in [%i:%i]', start, end)
            saccades = self._detect_saccades(
                None,
                data,
                start,
                end,
                context=None)
            saccade_events = []
            if saccades is not None:
                kill_pso = False
                for s in saccades:
                    if kill_pso:
                        kill_pso = False
                        if s['label'].endswith('PSO'):
                            continue
                    if s['start_time'] - start < self.min_intersac_dur or \
                            end - s['end_time'] < self.min_intersac_dur:
                        # to close to another saccade
                        kill_pso = True
                        continue
                    s['label'] = label_remap.get(s['label'], s['label'])
                    # need to make a copy of the dict to not have outside
                    # modification interfere with further inside processing
                    yield s.copy()
                    saccade_events.append(s)
            if saccade_events:
                lgr.warn('Found additional saccades in ISP')
                # and now process the intervals between the saccades
                for e in self._classify_intersaccade_periods(
                        data,
                        start,
                        end,
                        sorted(saccade_events,
                               key=lambda x: x['start_time']),
                        saccade_detection=False):
                    yield e
                return

        max_amp, label = self._fix_or_pursuit(data, start, end)
        if label is not None:
            yield self._mk_event_record(
                data,
                max_amp,
                label,
                start,
                end)

    def _fix_or_pursuit(self, data, start, end):
        win_data = data[start:end].copy()

        if len(win_data) < self.min_fix_dur:
            return None, None

        def _butter_lowpass(cutoff, fs, order=5):
            nyq = 0.5 * fs
            normal_cutoff = cutoff / nyq
            b, a = signal.butter(
                order,
                normal_cutoff,
                btype='low',
                analog=False)
            return b, a

        b, a = _butter_lowpass(10.0, 1000.0)
        win_data['x'] = signal.filtfilt(b, a, win_data['x'], method='gust')
        win_data['y'] = signal.filtfilt(b, a, win_data['y'], method='gust')

        win_data = win_data[10:-10]
        start_x = win_data[0]['x']
        start_y = win_data[0]['y']

        # determine max location deviation from start coordinate
        amp = (((start_x - win_data['x']) ** 2 +
                (start_y - win_data['y']) ** 2) ** 0.5)
        amp_argmax = amp.argmax()
        max_amp = amp[amp_argmax] * self.px2deg
        #print('MAX IN WIN [{}:{}]@{:.1f})'.format(start, end, max_amp))

        if max_amp > self.max_fix_amp:
            return max_amp, 'PURS'
        return max_amp, 'FIXA'

    def preproc(
            self,
            data,
            min_blink_duration=0.02,
            dilate_nan=0.01,
            median_filter_length=0.05,
            savgol_length=0.019,
            savgol_polyord=2,
            max_vel=1000.0):
        """
        Parameters
        ----------
        data : array
          Record array with fields ('x', 'y', 'pupil')
        px2deg : float
          Size of a pixel in visual angles.
        min_blink_duration : float
          In seconds. Any signal loss shorter than this duration with not be
          considered for `dilate_blink`.
        dilate_blink : float
          Duration by which to dilate a blink window (missing data segment) on
          either side (in seconds).
        median_filter_width : float
          Filter window length in seconds.
        savgol_length : float
          Filter window length in seconds.
        savgol_polyord : int
          Filter polynomial order used to fit the samples.
        sampling_rate : float
          In Hertz
        max_vel : float
          Maximum velocity in deg/s. Any velocity value larger than this threshold
          will be replaced by the previous velocity value. Additionally a warning
          will be issued to indicate a potentially inappropriate filter setup.
        """
        # convert params in seconds to #samples
        dilate_nan = int(dilate_nan * self.sr)
        min_blink_duration = int(min_blink_duration * self.sr)
        savgol_length = int(savgol_length * self.sr)
        median_filter_length = int(median_filter_length * self.sr)

        # in-place spike filter
        data = filter_spikes(data)

        # for signal loss exceeding the minimum blink duration, add additional
        # dilate_nan at either end
        # find clusters of "no data"
        if dilate_nan:
            mask = get_dilated_nan_mask(
                data['x'],
                dilate_nan,
                min_blink_duration)
            data['x'][mask] = np.nan
            data['y'][mask] = np.nan

        if savgol_length:
            for i in ('x', 'y'):
                data[i] = savgol_filter(data[i], savgol_length, savgol_polyord)

        # velocity calculation, exclude velocities over `max_vel`
        # euclidean distance between successive coordinate samples
        # no entry for first datapoint!
        velocities = (np.diff(data['x']) ** 2 + np.diff(data['y']) ** 2) ** 0.5
        # convert from px/sample to deg/s
        velocities *= self.px2deg * self.sr

        if median_filter_length:
            med_velocities = np.zeros((len(data),), velocities.dtype)
            med_velocities[1:] = (
                np.diff(median_filter(data['x'],
                                      size=median_filter_length)) ** 2 +
                np.diff(median_filter(data['y'],
                                      size=median_filter_length)) ** 2) ** 0.5
            # convert from px/sample to deg/s
            med_velocities *= self.px2deg * self.sr
            # remove any velocity bordering NaN
            med_velocities[get_dilated_nan_mask(
                med_velocities, dilate_nan, 0)] = np.nan

        # replace "too fast" velocities with previous velocity
        # add missing first datapoint
        filtered_velocities = [float(0)]
        for vel in velocities:
            if vel > max_vel:  # deg/s
                # ignore very fast velocities
                lgr.warning(
                    'Computed velocity exceeds threshold. '
                    'Inappropriate filter setup? [%.1f > %.1f deg/s]',
                    vel,
                    max_vel)
                vel = filtered_velocities[-1]
            filtered_velocities.append(vel)
        velocities = np.array(filtered_velocities)

        # acceleration is change of velocities over the last time unit
        acceleration = np.zeros(velocities.shape, velocities.dtype)
        acceleration[1:] = (velocities[1:] - velocities[:-1]) * self.sr

        arrs = [med_velocities] if median_filter_length else []
        names = ['med_vel'] if median_filter_length else []
        arrs.extend([
            velocities,
            acceleration,
            data['x'],
            data['y']])
        names.extend(['vel', 'accel', 'x', 'y'])
        return np.core.records.fromarrays(arrs, names=names)


if __name__ == '__main__':
    fixation_velthresh = float(sys.argv[1])
    px2deg = float(sys.argv[2])
    infpath = sys.argv[3]
    outfpath = sys.argv[4]
    data = np.recfromcsv(
        infpath,
        delimiter='\t',
        names=['vel', 'accel', 'x', 'y'])

    events = detect(data, outfpath, fixation_velthresh, px2deg)

    # TODO think about just saving it in binary form
    f = gzip.open(outfpath, "w")
    for e in events:
        f.write('%s\t%i\t%i\t%f\t%f\t%f\t%f\t%f\t%f\t%f\t%f\n' % e)






#Selection criterion for IVT threshold

#@inproceedings{Olsen:2012:IPV:2168556.2168625,
#author = {Olsen, Anneli and Matos, Ricardo},
#title = {Identifying Parameter Values for an I-VT Fixation Filter Suitable for Handling Data Sampled with Various Sampling Frequencies},
# booktitle = {Proceedings of the Symposium on Eye Tracking Research and Applications},
#series = {ETRA '12},
#year = {2012},
#isbn = {978-1-4503-1221-9},
#location = {Santa Barbara, California},
#pages = {317--320},
#numpages = {4},
#url = {http://doi.acm.org/10.1145/2168556.2168625},
#doi = {10.1145/2168556.2168625},
#acmid = {2168625},
#publisher = {ACM},
#address = {New York, NY, USA},
#keywords = {algorithm, classification, eye movements, scoring},
#} 

#Human-Computer Interaction: Psychonomic Aspects
#edited by Gerrit C. van der Veer, Gijsbertus Mulder
#pg 58-59

#Eye Tracking: A comprehensive guide to methods and measures: Rotting (2001)
#By Kenneth Holmqvist, Marcus Nystrom, Richard Andersson, Richard Dewhurst, Halszka Jarodzka, Joost van de Weijer

#A good reveiw along with a great chunk of the content found in this code:
#@Article{Nystr├Âm2010,
#author="Nystr{\"o}m, Marcus
#and Holmqvist, Kenneth",
#title="An adaptive algorithm for fixation, saccade, and glissade detection in eyetracking data",
#journal="Behavior Research Methods",
#year="2010",
#month="Feb",
#day="01",
#volume="42",
#number="1",
#pages="188--204",
#abstract="Event detection is used to classify recorded gaze points into periods of fixation, saccade, smooth pursuit, blink, and noise. Although there is an overall consensus that current algorithms for event detection have serious flaws and that a de facto standard for event detection does not exist, surprisingly little work has been done to remedy this problem. We suggest a new velocity-based algorithm that takes several of the previously known limitations into account. Most important, the new algorithm identifies so-called glissades, a wobbling movement at the end of many saccades, as a separate class of eye movements. Part of the solution involves designing an adaptive velocity threshold that makes the event detection less sensitive to variations in noise level and the algorithm settings-free for the user. We demonstrate the performance of the new algorithm on eye movements recorded during reading and scene perception and compare it with two of the most commonly used algorithms today. Results show that, unlike the currently used algorithms, fixations, saccades, and glissades are robustly identified by the new algorithm. Using this algorithm, we found that glissades occur in about half of the saccades, during both reading and scene perception, and that they have an average duration close to 24 msec. Due to the high prevalence and long durations of glissades, we argue that researchers must actively choose whether to assign the glissades to saccades or fixations; the choice affects dependent variables such as fixation and saccade duration significantly. Current algorithms do not offer this choice, and their assignments of each glissade are largely arbitrary.",
#issn="1554-3528",
#doi="10.3758/BRM.42.1.188",
#url="https://doi.org/10.3758/BRM.42.1.188"
