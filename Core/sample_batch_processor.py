"""Batch processing for audio samples.

Handles:
- Batch operations on multiple files
- Processing pipelines
- Progress tracking
- Error handling and recovery
"""
from __future__ import annotations
from pathlib import Path
from typing import List, Optional, Callable
from dataclasses import dataclass
import logging

from Core.sample_editor import SampleEditor, SampleSelection

log = logging.getLogger(__name__)


@dataclass
class BatchJob:
    """Single batch processing job."""
    input_file: Path
    output_file: Path
    operations: List[str]  # Operation names
    parameters: dict = None  # Operation parameters


class BatchProcessor:
    """Process multiple audio files with the same operations."""

    def __init__(self):
        """Initialize batch processor."""
        self.is_processing = False
        self.current_job_idx = 0
        self.total_jobs = 0
        self.progress_callback: Optional[Callable[[float, str], None]] = None
        self.error_callback: Optional[Callable[[str, str], None]] = None

    def process_batch(self, jobs: List[BatchJob]) -> int:
        """Process a batch of files.

        Args:
            jobs: List of batch jobs to process

        Returns:
            Number of successfully processed files
        """
        self.is_processing = True
        self.total_jobs = len(jobs)
        self.current_job_idx = 0
        successful = 0

        for idx, job in enumerate(jobs):
            self.current_job_idx = idx
            try:
                self._emit_progress(idx / len(jobs), f"Processing {job.input_file.name}")
                if self._process_job(job):
                    successful += 1
            except Exception as e:
                error_msg = f"Error processing {job.input_file.name}: {e}"
                log.error(error_msg)
                if self.error_callback:
                    self.error_callback(job.input_file.name, str(e))

        self.is_processing = False
        self._emit_progress(1.0, f"Complete: {successful}/{len(jobs)} files")
        return successful

    def _process_job(self, job: BatchJob) -> bool:
        """Process a single batch job.

        Args:
            job: Job to process

        Returns:
            True if successful
        """
        try:
            from Core.audio_sample_player import WavFileReader
            from Core.audio_recorder import WavFileWriter

            # Load file
            info = WavFileReader.read_header(job.input_file)
            if not info:
                log.error(f"Cannot read {job.input_file}")
                return False

            audio_data = WavFileReader.read_samples(
                job.input_file, 0, info.data_size
            )
            if not audio_data:
                return False

            # Apply operations
            for operation in job.operations:
                audio_data = self._apply_operation(
                    audio_data, operation, info.sample_rate, info.channels, job.parameters
                )

            # Save file
            writer = WavFileWriter(
                job.output_file, info.sample_rate, info.channels, bit_depth=16
            )
            if writer.open():
                writer.write(audio_data)
                writer.close()
                log.info(f"Processed: {job.output_file.name}")
                return True

        except Exception as e:
            log.error(f"Job processing failed: {e}")
            return False

        return False

    def _apply_operation(self, audio_data: bytes, operation: str,
                        sample_rate: int, channels: int,
                        parameters: dict) -> bytes:
        """Apply a single operation to audio.

        Args:
            audio_data: Audio to process
            operation: Operation name
            sample_rate: Sample rate in Hz
            channels: Number of channels
            parameters: Operation parameters

        Returns:
            Processed audio data
        """
        if not parameters:
            parameters = {}

        try:
            if operation == "normalize":
                return SampleEditor.normalize(audio_data, parameters.get("target_db", -3.0))
            elif operation == "reverse":
                return SampleEditor.reverse(audio_data, sample_rate, channels)
            elif operation == "gain":
                return SampleEditor.apply_gain(audio_data, parameters.get("gain_db", 0.0))
            elif operation == "fade_in":
                return SampleEditor.fade_in(
                    audio_data, parameters.get("duration_sec", 1.0),
                    sample_rate, channels, parameters.get("curve", "linear")
                )
            elif operation == "fade_out":
                return SampleEditor.fade_out(
                    audio_data, parameters.get("duration_sec", 1.0),
                    sample_rate, channels, parameters.get("curve", "linear")
                )
            else:
                log.warning(f"Unknown operation: {operation}")
                return audio_data

        except Exception as e:
            log.error(f"Operation '{operation}' failed: {e}")
            return audio_data

    def _emit_progress(self, progress: float, status: str):
        """Emit progress update.

        Args:
            progress: Progress 0-1
            status: Status message
        """
        if self.progress_callback:
            self.progress_callback(progress, status)


class ProcessingPipeline:
    """Reusable processing pipeline."""

    def __init__(self, name: str):
        """Initialize pipeline.

        Args:
            name: Pipeline name
        """
        self.name = name
        self.operations: List[tuple[str, dict]] = []

    def add_operation(self, operation: str, **parameters):
        """Add operation to pipeline.

        Args:
            operation: Operation name
            **parameters: Operation parameters
        """
        self.operations.append((operation, parameters))
        log.info(f"Added {operation} to pipeline {self.name}")

    def to_batch_jobs(self, input_dir: Path, output_dir: Path,
                     pattern: str = "*.wav") -> List[BatchJob]:
        """Generate batch jobs from input directory.

        Args:
            input_dir: Input directory
            output_dir: Output directory
            pattern: File pattern to match

        Returns:
            List of batch jobs
        """
        jobs = []
        input_dir = Path(input_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        for input_file in input_dir.glob(pattern):
            output_file = output_dir / input_file.name
            job = BatchJob(
                input_file=input_file,
                output_file=output_file,
                operations=[op[0] for op in self.operations],
                parameters={op[0]: op[1] for op in self.operations}
            )
            jobs.append(job)

        return jobs

    def save(self, filepath: Path):
        """Save pipeline to file.

        Args:
            filepath: Path to save to
        """
        import json

        data = {
            "name": self.name,
            "operations": [
                {"name": op[0], "parameters": op[1]}
                for op in self.operations
            ]
        }

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            log.info(f"Saved pipeline to {filepath}")
        except Exception as e:
            log.error(f"Failed to save pipeline: {e}")

    @classmethod
    def load(cls, filepath: Path) -> ProcessingPipeline:
        """Load pipeline from file.

        Args:
            filepath: Path to load from

        Returns:
            Loaded pipeline
        """
        import json

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            pipeline = cls(data["name"])
            for op in data["operations"]:
                pipeline.add_operation(op["name"], **op["parameters"])

            return pipeline

        except Exception as e:
            log.error(f"Failed to load pipeline: {e}")
            return cls("untitled")
