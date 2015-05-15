"""Thread-based objects that manage data as it arrives from the scanner."""
from __future__ import print_function, division
from threading import Thread
from Queue import Empty
from time import sleep
import logging

import numpy as np
import nibabel as nib


logger = logging.getLogger(__name__)


class Finder(Thread):
    """Base class that uses a slightly different approach to thread control."""
    def __init__(self, interval):
        """Initialize the Finder."""
        super(Finder, self).__init__()

        self.interval = interval
        self.alive = True

    def halt(self):
        """Make it so the thread will halt within a run method."""
        self.alive = False


class SeriesFinder(Finder):
    """Manage a queue of series directories on the scanner.

    The queue will only be populated with series that look like they
    are timeseries, because that is what is useful for real-time analysis.

    """
    def __init__(self, scanner, queue, interval=1, skip=0):
        """Initialize the queue."""
        super(SeriesFinder, self).__init__(interval)

        self.scanner = scanner
        self.current_series = None
        self.skip = None
        self.skip_set = set()

        self.queue = queue
        self.alive = True

    def put_if_timeseries(self, series, min_timepoints=6):
        """Check if series appears 4D and add to the queue if so."""
        latest_info = self.scanner.series_info(series)
        if latest_info["NumTimepoints"] > min_timepoints:
            logger.debug(("Series appears to be 4D; adding to series queue"))
            self.queue.put(series)
        else:
            logger.debug(("Series does not appear to be 4D; skipping"))

    def should_skip(self, series):
        """Determine if this is an early series that should be skipped."""
        if series in self.skip_set:
            return True
        elif len(self.skip_set) < self.skip:
            self.skip_set.add(series)
            logger.debug(("Skipping series"))
            return True
        return False

    def run(self):
        """This function gets looped over repetedly while thread is alive."""
        while self.alive:

            if self.current_series is None:
                logger.debug("Starting series collection")

                # Load up all series for the current exam
                for series in self.scanner.series_dirs():

                    logger.debug("Checking series {}".format(series))

                    if self.should_skip(series):
                        continue

                    self.put_if_timeseries(series)

                self.current_series = series
            else:
                # Only do anything if there's a new series
                latest_series = self.scanner.latest_series
                if self.current_series != latest_series:

                    self.current_series = latest_series
                    logger.debug("Found new series: {}".format(series))

                    if self.should_skip(latest_series):
                        continue

                    self.put_if_timeseries(series)

            sleep(self.interval)


class DicomFinder(Finder):
    """Manage a queue of DICOM files on the scanner.

    This class talks to the scanner and to a separately-managed series queue.

    Note
    ----

    The queue order will reflect the timestamps and filenames of the dicom
    files on the scanner. This is *not* guaranteed to order the files in the
    actual order of acquisition. Downstream components of the processing
    pipeline should inspect the files for metadata that can be used to
    put them in the right order.

    """
    def __init__(self, scanner, series_q, dicom_q, interval=1):
        """Initialize the queue."""
        super(DicomFinder, self).__init__(interval)

        # Referneces to the external objects we need to talk to
        self.scanner = scanner
        self.series_q = series_q
        self.dicom_q = dicom_q

        # We'll want to keep track of the current series
        self.current_series = None

        # A set to keep track of files we've added onto the queue
        # (This is needed because we'll likely be running over the same
        # series directory multiple times, so we need to know what's in
        # the queue). We use a set because the relevant operations are
        # quite a bit faster than they would be with lists.
        self.dicom_files = set()

    def run(self):
        """This function gets looped over repetedly while thread is alive."""
        while self.alive:

            if self.current_series is not None:

                # Find all the dicom files in this series
                series_files = self.scanner.series_files(self.current_series)

                # Compare against the set of files we've already placed
                # in the queue, keep only the new ones
                new_files = [f for f in series_files
                             if f not in self.dicom_files]

                if new_files:
                    logger.debug(("Putting {:d} files into dicom queue"
                                  .format(len(new_files))))

                # Place each new file onto the queue
                for fname in new_files:
                    self.dicom_q.put(self.scanner.retrieve_dicom(fname))

                # Update the set of files on the queue
                self.dicom_files.update(set(new_files))

            if not self.series_q.empty():

                # Grab the next series path off the queue
                self.current_series = self.series_q.get()

                logger.debug(("Beginning DICOM collection for new series: {}"
                              .format(self.current_series)))

                # Reset the set of dicom files. Once we've moved on to
                # the next series, we don't need to track these any more
                # and this keeps it from growing too large
                self.dicom_files = set()

            sleep(self.interval)


class Volumizer(Finder):
    """Reconstruct MRI volumes and manage a queue of them.

    This class talks to the Dicom queue, but does not need to talk to
    the scanner.

    """
    def __init__(self, dicom_q, volume_q, interval=1):
        """Initialize the queue."""
        super(Volumizer, self).__init__(interval)

        # The external queue objects we are talking to
        self.dicom_q = dicom_q
        self.volume_q = volume_q

    def generate_affine_matrix(self, dcm):
        """Use DICOM metadata to generate an affine matrix."""
        # The notes in the original code say this has to be done because
        # dicom.get_affine() doesn't work. I think that's referring to
        # nibabel.nicom.dicomwrappers.Wrapper, which comes with a warning
        # that it only works for Siemens files. This method can probably be
        # eliminated in the future when nibabel works with GE files.

        # Begin with an identity matrix
        affine = np.eye(4)

        # Figure out the three dimensions of the voxels
        if ("PixelSpacing" in dcm) and ("SpacingBetweenSlices" in dcm):
            x, y = dcm.PixelSpacing
            z = dcm.SpacingBetweenSlices
            mm_per_vox = list(map(float, [x, y, z]))
        else:
            mm_per_vox = [0.] * 3
        affine[:3, :3] = np.diag(mm_per_vox)

        # Get the patient position
        x, y, z = dcm.ImagePositionPatient
        affine[:3, 3] = -float(x), -float(y), float(z)

        return affine

    def dicom_esa(self, dcm):
        """Extract the exam, series, and acquisition metadata.

        These three values will uniquely identiy the scanner run.

        """
        exam = int(dcm.StudyID)
        series = int(dcm.SeriesNumber)
        acquisition = int(dcm.AcquisitionNumber)

        return exam, series, acquisition

    def assemble_volume(self, slices):
        """Turn a list of dicom slices into a nibabel volume and metadata."""
        dcm = slices[0]

        # Build the array of image data
        x, y = dcm.pixel_array.shape
        image_data = np.empty((x, y, len(slices)))
        for z, slice in enumerate(slices):
            image_data[..., z] = slice.pixel_array

        # Turn it into a nibabel object
        affine = self.generate_affine_matrix(dcm)
        image_object = nib.Nifti1Image(image_data, affine)

        # Build the volume dictionary we will put in the dicom queue
        exam, series, acquisition = self.dicom_esa(dcm)
        volume = dict(
            exam=exam,
            series=series,
            acquisition=acquisition,
            patient_id=dcm.PatientID,
            series_description=dcm.SeriesDescription,
            tr=float(dcm.RepetitionTime) / 1000,
            ntp=float(dcm.NumberOfTemporalPositions),
            image=image_object
            )

        return volume

    def run(self):
        """This function gets looped over repetedly while thread is alive."""
        # Initialize the list we'll using to track progress
        instance_numbers_needed = None
        instance_numbers_gathered = []
        current_esa = None
        current_slices = []

        while self.alive:

            try:
                dcm = self.dicom_q.get(timeout=1)
            except Empty:
                sleep(self.interval)
                continue

            try:
                # This is the dicom tag for "Number of locations"
                slices_per_volume = dcm[(0x0021, 0x104f)].value
            except KeyError:
                # TODO In theory, we shouldn't get to here, because the
                # series queue should only have timeseries images in it.
                # Need to figure out under what circumstances we'd get a
                # file that doesn't have a "number of locations" tag,
                # and what we should do with it when we do.
                # The next line is just taken from the original code.
                slices_per_volume = getattr(dcm, "ImagesInAcquisition")
            slices_per_volume = int(slices_per_volume)

            # Determine if this is a slice from a new acquisition
            this_esa = self.dicom_esa(dcm)
            if current_esa is None or this_esa != current_esa:
                # Begin tracking the slices we need for the first volume
                # from this acquisition
                instance_numbers_needed = np.arange(slices_per_volume) + 1
                current_esa = this_esa
                logger.debug(("Collecting slices for new scanner run - "
                              "(exam: {} series: {} acquisition: {})"
                              .format(*current_esa)))

            # Get the DICOM instance for this volume
            # This is an incremental index that reflects position in time
            # and space (i.e. the index is the same for interleaved or
            # not sequential acquisitisions) so we can trust it to put
            # the volumes in the correct order.
            current_slice = int(dcm.InstanceNumber)

            # Add this slice index and dicom object to current list
            instance_numbers_gathered.append(current_slice)
            current_slices.append(dcm)

            if set(instance_numbers_needed) <= set(instance_numbers_gathered):

                # Files are not guaranteed to enter the DICOM queue in any
                # particular order. If we get here, then we have picked up
                # all the slices we need for this volume, but they might be
                # out of order, and we might have other slices that belong to
                # the next volume. So we need to figure out the correct order
                # and then extract what we need, leaving the rest to be dealt
                # with later.

                volume_slices = []
                for slice_number in instance_numbers_needed:
                    slice_index = instance_numbers_gathered.index(slice_number)
                    volume_slices.append(current_slices.pop(slice_index))
                    instance_numbers_gathered.pop(slice_index)

                # Assemble all the slices together into a nibabel object
                logger.debug(("Assembling full volume for slices {:d}-{:d}"
                              .format(min(instance_numbers_needed),
                                      max(instance_numbers_needed))))
                volume = self.assemble_volume(volume_slices)

                # Put that object on the dicom queue
                self.volume_q.put(volume)

                # Update the array of slices we need for the next volume
                instance_numbers_needed += slices_per_volume
