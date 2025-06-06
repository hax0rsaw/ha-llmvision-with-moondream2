# media_handlers.py
import base64
import io
import os
import uuid
import shutil
import logging
import time
import asyncio
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from functools import partial
from bisect import insort
from PIL import Image, UnidentifiedImageError
import numpy as np
from homeassistant.helpers.network import get_url
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MediaProcessor:
    def __init__(self, hass, client):
        self.hass = hass
        self.session = async_get_clientsession(self.hass)
        self.client = client
        self.base64_images = []
        self.filenames = []
        self.ssim_scores = []  # Add SSIM scores tracking for better image selection
        self.path = self.hass.config.path(f"www/{DOMAIN}")
        self.key_frame = ""

    async def _encode_image(self, img):
        """Encode image as base64"""
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG')
        base64_image = base64.b64encode(
            img_byte_arr.getvalue()).decode('utf-8')
        return base64_image

    async def _save_clip(self, clip_data=None, clip_path=None, image_data=None, image_path=None):
        # Ensure dir exists
        await self.hass.loop.run_in_executor(None, partial(os.makedirs, self.path, exist_ok=True))

        def _run_save_clips(clip_data, clip_path, image_data, image_path):
            _LOGGER.info(f"[save_clip] clip: {clip_path}, image: {image_path}")
            if image_data:
                with open(image_path, "wb") as f:
                    if type(image_data) == bytes:
                        f.write(image_data)
                    else:
                        f.write(base64.b64decode(image_data))
            elif clip_data:
                with open(clip_path, "wb") as f:
                    f.write(clip_data)
        await self.hass.loop.run_in_executor(None, _run_save_clips, clip_data, clip_path, image_data, image_path)

    def _convert_to_rgb(self, img):
        if img.mode == 'RGBA' or img.format == 'GIF':
            img = img.convert('RGB')
        return img

    async def _expose_image(self, frame_name, image_data, uid, frame_path=None):
        # ensure /www/llmvision dir exists
        await self.hass.loop.run_in_executor(None, partial(os.makedirs, self.hass.config.path(f"www/{DOMAIN}"), exist_ok=True))
        if self.key_frame == "":
            filename = self.hass.config.path(
                f"www/{DOMAIN}/{uid}-{frame_name}.jpg")
            self.key_frame = filename
            if image_data is None and frame_path is not None:
                # open image in hass.loop
                with await self.hass.loop.run_in_executor(None, Image.open, frame_path) as image:
                    await self.hass.loop.run_in_executor(None, image.load)
                    image_data = await self._encode_image(image)
            await self._save_clip(image_data=image_data, image_path=filename)

    def _similarity_score(self, previous_frame, current_frame_gray):
        """
        SSIM by Z. Wang: https://ece.uwaterloo.ca/~z70wang/research/ssim/
        Paper:  Z. Wang, A. C. Bovik, H. R. Sheikh and E. P. Simoncelli,
        "Image quality assessment: From error visibility to structural similarity," IEEE Transactions on Image Processing, vol. 13, no. 4, pp. 600-612, Apr. 2004.
        """
        K1 = 0.005
        K2 = 0.015
        L = 255

        C1 = (K1 * L) ** 2
        C2 = (K2 * L) ** 2

        previous_frame_np = np.array(previous_frame)
        current_frame_np = np.array(current_frame_gray)

        # Ensure both frames have same dimensions
        if previous_frame_np.shape != current_frame_np.shape:
            min_shape = np.minimum(
                previous_frame_np.shape, current_frame_np.shape)
            previous_frame_np = previous_frame_np[:min_shape[0], :min_shape[1]]
            current_frame_np = current_frame_np[:min_shape[0], :min_shape[1]]

        # Calculate mean (mu)
        mu1 = np.mean(previous_frame_np, dtype=np.float64)
        mu2 = np.mean(current_frame_np, dtype=np.float64)

        # Calculate variance (sigma^2) and covariance (sigma12)
        sigma1_sq = np.var(previous_frame_np, dtype=np.float64, mean=mu1)
        sigma2_sq = np.var(current_frame_np, dtype=np.float64, mean=mu2)
        sigma12 = np.cov(previous_frame_np.flatten(),
                         current_frame_np.flatten(),
                         dtype=np.float64)[0, 1]

        # Calculate SSIM
        ssim = ((2 * mu1 * mu2 + C1) * (2 * sigma12 + C2)) / \
            ((mu1**2 + mu2**2 + C1) * (sigma1_sq + sigma2_sq + C2))

        return ssim

    async def resize_image(self, target_width, image_path=None, image_data=None, img=None):
        """Resize image to target_width"""
        if image_path:
            # Open the image file
            img = await self.hass.loop.run_in_executor(None, Image.open, image_path)
            with img:
                await self.hass.loop.run_in_executor(None, img.load)
                # Check if the image is a GIF and convert if necessary
                img = self._convert_to_rgb(img)
                # calculate new height based on aspect ratio
                width, height = img.size
                aspect_ratio = width / height
                target_height = int(target_width / aspect_ratio)

                # Resize the image only if it's larger than the target size
                if width > target_width or height > target_height:
                    img = img.resize((target_width, target_height))

                # Encode the image to base64
                base64_image = await self._encode_image(img)

        elif image_data:
            # Convert the image to base64
            img_byte_arr = io.BytesIO()
            img_byte_arr.write(image_data)
            img = await self.hass.loop.run_in_executor(None, Image.open, img_byte_arr)
            with img:
                await self.hass.loop.run_in_executor(None, img.load)
                img = self._convert_to_rgb(img)
                # calculate new height based on aspect ratio
                width, height = img.size
                aspect_ratio = width / height
                target_height = int(target_width / aspect_ratio)

                if width > target_width or height > target_height:
                    img = img.resize((target_width, target_height))

                base64_image = await self._encode_image(img)
        elif img:
            with img:
                img = self._convert_to_rgb(img)
                # calculate new height based on aspect ratio
                width, height = img.size
                aspect_ratio = width / height
                target_height = int(target_width / aspect_ratio)

                if width > target_width or height > target_height:
                    img = img.resize((target_width, target_height))

                base64_image = await self._encode_image(img)

        return base64_image

    async def _fetch(self, url, max_retries=2, retry_delay=1):
        """Fetch image from url and return image data"""
        retries = 0
        while retries < max_retries:
            _LOGGER.info(
                f"Fetching {url} (attempt {retries + 1}/{max_retries})")
            try:
                response = await self.session.get(url)
                if response.status != 200:
                    _LOGGER.warning(
                        f"Couldn't fetch frame (status code: {response.status})")
                    retries += 1
                    await asyncio.sleep(retry_delay)
                    continue
                data = await response.read()
                return data
            except Exception as e:
                _LOGGER.error(f"Fetch failed: {e}")
                retries += 1
                await asyncio.sleep(retry_delay)
        _LOGGER.warning(f"Failed to fetch {url} after {max_retries} retries")

    async def record(self, image_entities, duration, max_frames, target_width, include_filename, expose_images):
        """Wrapper for client.add_frame with integrated recorder

        Args:
            image_entities (list[string]): List of camera entities to record
            duration (float): Duration in seconds to record
            target_width (int): Target width for the images in pixels
        """

        interval = 1 if duration < 3 else 2 if duration < 10 else 4 if duration < 30 else 6 if duration < 60 else 10
        camera_frames = {}

        # Record on a separate thread for each camera
        async def record_camera(image_entity, camera_number):
            start = time.time()
            frame_counter = 0
            frames = {}
            previous_frame = None
            iteration_time = 0

            while time.time() - start < duration + iteration_time:
                fetch_start_time = time.time()
                base_url = get_url(self.hass)
                frame_url = base_url + \
                    self.hass.states.get(image_entity).attributes.get(
                        'entity_picture')
                frame_data = await self._fetch(frame_url)

                # Skip frame if fetch failed
                if not frame_data:
                    continue

                fetch_duration = time.time() - fetch_start_time
                _LOGGER.info(
                    f"Fetched {image_entity} in {fetch_duration:.2f} seconds")

                preprocessing_start_time = time.time()

                with await self.hass.loop.run_in_executor(None, Image.open, io.BytesIO(frame_data)) as img:
                    current_frame_gray = np.array(img.convert('L'))

                    if previous_frame is not None:
                        score = self._similarity_score(
                            previous_frame, current_frame_gray)

                        # Encode the image back to bytes
                        buffer = io.BytesIO()
                        img.save(buffer, format="JPEG")
                        frame_data = buffer.getvalue()

                        # Use either entity name or assign number to each camera
                        frame_label = (image_entity.replace("camera.", "") + " frame " + str(frame_counter)
                                       if include_filename else "camera " + str(camera_number) + " frame " + str(frame_counter))
                        frames.update(
                            {frame_label: {"frame_data": frame_data, "ssim_score": score}})

                        frame_counter += 1
                        previous_frame = current_frame_gray
                    else:
                        # Initialize previous_frame with the first frame
                        previous_frame = current_frame_gray
                        # First snapshot of the camera, always considered important.
                        score = -9999

                preprocessing_duration = time.time() - preprocessing_start_time
                _LOGGER.info(
                    f"Preprocessing took: {preprocessing_duration:.2f} seconds")

                adjusted_interval = max(
                    0, interval - fetch_duration - preprocessing_duration)

                if iteration_time == 0:
                    iteration_time = time.time() - start
                    _LOGGER.info(
                        f"First iteration took: {iteration_time:.2f} seconds, interval adjusted to: {adjusted_interval}")

                await asyncio.sleep(adjusted_interval)

            camera_frames.update({image_entity: frames})

        _LOGGER.info(f"Recording {', '.join([entity.replace(
            'camera.', '') for entity in image_entities])} for {duration} seconds")

        # start threads for each camera
        await asyncio.gather(*(record_camera(image_entity, image_entities.index(image_entity)) for image_entity in image_entities))

        # Extract frames and their SSIM scores
        frames_with_scores = []
        for frame in camera_frames:
            for frame_name, frame_data in camera_frames[frame].items():
                frames_with_scores.append(
                    (frame_name, frame_data["frame_data"], frame_data["ssim_score"]))

        # Sort frames by SSIM score
        frames_with_scores.sort(key=lambda x: x[2])

        # Select frames with lowest ssim SIM scores
        selected_frames = frames_with_scores[:max_frames]
        # Sort selected frames back into their original chronological order
        selected_frames.sort(key=lambda x: x[0])

        # Add selected frames to client
        for frame_name, frame_data, ssim_score in selected_frames:
            resized_image = await self.resize_image(target_width=target_width, image_data=frame_data)
            if expose_images:
                await self._expose_image(frame_name[-1], resized_image, uid=str(uuid.uuid4())[:8])

            self.client.add_frame(
                base64_image=resized_image,
                filename=frame_name,
                ssim_score=ssim_score  # Pass SSIM score for Moondream selection
            )

    async def add_images(self, image_entities, image_paths, target_width, include_filename, expose_images):
        """Wrapper for client.add_frame for images"""
        if image_entities:
            for image_entity in image_entities:
                try:
                    base_url = get_url(self.hass)
                    entity_state = self.hass.states.get(image_entity)
                    if not entity_state:
                        raise ServiceValidationError(f"Entity {image_entity} does not exist")
                    
                    entity_picture = entity_state.attributes.get('entity_picture')
                    if not entity_picture:
                        raise ServiceValidationError(f"Entity {image_entity} does not have an entity_picture attribute")
                    
                    image_url = base_url + entity_picture
                    image_data = await self._fetch(image_url)

                    # Skip frame if fetch failed
                    if not image_data:
                        if len(image_entities) == 1:
                            raise ServiceValidationError(
                                f"Failed to fetch image from {image_entity}")

                    # If entity snapshot requested, use entity name as 'filename'
                    resized_image = await self.resize_image(target_width=target_width, image_data=image_data)
                    self.client.add_frame(
                        base64_image=resized_image,
                        filename=entity_state.attributes.get('friendly_name') if include_filename else "",
                        ssim_score=0.0  # Default SSIM score for single images
                    )

                    if expose_images:
                        await self._expose_image("0", resized_image, str(uuid.uuid4())[:8])

                except AttributeError as e:
                    raise ServiceValidationError(f"Entity {image_entity} does not exist")
                except Exception as e:
                    raise ServiceValidationError(f"Error processing {image_entity}: {e}")
                    
        if image_paths:
            for image_path in image_paths:
                try:
                    image_path = image_path.strip()
                    if include_filename and os.path.exists(image_path):
                        self.client.add_frame(
                            base64_image=await self.resize_image(target_width=target_width, image_path=image_path),
                            filename=image_path.split('/')[-1].split('.')[-2],
                            ssim_score=0.0  # Default SSIM score for single images
                        )
                    elif os.path.exists(image_path):
                        self.client.add_frame(
                            base64_image=await self.resize_image(target_width=target_width, image_path=image_path),
                            filename="",
                            ssim_score=0.0  # Default SSIM score for single images
                        )
                    if not os.path.exists(image_path):
                        raise ServiceValidationError(
                            f"File {image_path} does not exist")
                    if expose_images:
                        await self._expose_image("0", await self.resize_image(target_width=target_width, image_path=image_path), str(uuid.uuid4())[:8])
                except Exception as e:
                    raise ServiceValidationError(f"Error: {e}")
        return self.client

    async def add_videos(self, video_paths, event_ids, max_frames, target_width, include_filename, expose_images, frigate_retry_attempts, frigate_retry_seconds):
        """Wrapper for client.add_frame for videos"""
        tmp_clips_dir = self.hass.config.path(
            f"custom_components/{DOMAIN}/tmp_clips")
        tmp_frames_dir = self.hass.config.path(
            f"custom_components/{DOMAIN}/tmp_frames")
        processed_event_ids = []

        if not video_paths:
            video_paths = []
        if event_ids:
            for event_id in event_ids:
                try:
                    base_url = get_url(self.hass)
                    frigate_url = base_url + "/api/frigate/notifications/" + event_id + "/clip.mp4"

                    clip_data = await self._fetch(frigate_url, max_retries=frigate_retry_attempts, retry_delay=frigate_retry_seconds)

                    if not clip_data:
                        raise ServiceValidationError(
                            f"Failed to fetch frigate clip {event_id}")

                    # create tmp dir to store video clips
                    await self.hass.loop.run_in_executor(None, partial(os.makedirs, tmp_clips_dir, exist_ok=True))
                    # save clip to file with event_id as filename
                    clip_path = os.path.join(
                        tmp_clips_dir, event_id + ".mp4")
                    await self._save_clip(clip_data, clip_path)
                    _LOGGER.info(
                        f"Saved frigate clip to {clip_path} (temporarily)")
                    # append to video_paths
                    video_paths.append(clip_path)
                    processed_event_ids.append(event_id)

                except AttributeError as e:
                    raise ServiceValidationError(
                        f"Failed to fetch frigate clip {event_id}: {e}")

        _LOGGER.debug(f"Processing videos: {video_paths}")
        for video_path in video_paths:
            try:
                current_event_id = str(uuid.uuid4())
                processed_event_ids.append(current_event_id)
                video_path = video_path.strip()
                if os.path.exists(video_path):
                    # create tmp dir to store extracted frames
                    await self.hass.loop.run_in_executor(None, partial(os.makedirs, tmp_frames_dir, exist_ok=True))
                    if os.path.exists(tmp_frames_dir):
                        _LOGGER.debug(f"Created {tmp_frames_dir}")
                    else:
                        _LOGGER.error(
                            f"Failed to create temp directory {tmp_frames_dir}")

                    # Extract iframes from video
                    # use %05d formatting to enable iteration in sorted order
                    ffmpeg_cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-hwaccel", "auto",
                        "-skip_frame", "nokey",
                        "-an", "-sn", "-dn",
                        "-i", f"'{video_path}'",
                        "-fps_mode", "passthrough",
                        os.path.join(tmp_frames_dir, "frame%05d.jpg")
                    ]
                    # Run ffmpeg command
                    await self.hass.loop.run_in_executor(None, os.system, " ".join(ffmpeg_cmd))

                    previous_frame, previous_frame_path = None, None
                    frames = []

                    # Iterate over frames in sorted order
                    for frame_file in sorted(await self.hass.loop.run_in_executor(None, os.listdir, tmp_frames_dir)):
                        _LOGGER.debug(f"Adding frame {frame_file}")
                        frame_path = os.path.join(
                            tmp_frames_dir, frame_file)
                        try:
                            # open image in hass.loop
                            with await self.hass.loop.run_in_executor(None, Image.open, frame_path) as img:
                                await self.hass.loop.run_in_executor(None, img.load)
                                # Remove transparency for compatibility
                                if img.mode == 'RGBA':
                                    img = img.convert('RGB')
                                    await self.hass.loop.run_in_executor(None, img.save, frame_path)

                                current_frame_gray = np.array(img.convert('L'))

                            # Calculate similarity score
                            if previous_frame is not None:
                                score = self._similarity_score(
                                    previous_frame, current_frame_gray)
                                # Insert the new frame, maintain sorted order
                                insort(frames, (previous_frame_path,
                                       score), key=lambda x: x[1])
                                if len(frames) > max_frames:
                                    # Keep only max_frames many frames with lowest SSIM scores
                                    frames.pop()
                            previous_frame = current_frame_gray
                            previous_frame_path = frame_path
                        except UnidentifiedImageError:
                            _LOGGER.error(
                                f"Cannot identify image file {frame_path}")
                            continue

                    if len(frames) == 0 and previous_frame_path is not None:
                        frames.append((previous_frame_path, 0))

                    if expose_images:
                        # Expose images with original size, keep SSIM score order
                        for (frame_path, _) in frames:
                            frame_name = os.path.splitext(os.path.basename(frame_path))[
                                0].replace("frame", "")
                            await self._expose_image(frame_name, None, current_event_id[:8], frame_path)

                    # Add frames to client, sorted by frame number instead of SSIM score
                    for counter, (frame_path, ssim_score) in enumerate(sorted(frames, key=lambda x: x[0]), start=1):
                        resized_image = await self.resize_image(image_path=frame_path, target_width=target_width)
                        self.client.add_frame(
                            base64_image=resized_image,
                            filename=f"{os.path.splitext(os.path.basename(video_path))[0]} (frame {counter})" if include_filename else f"Video frame {counter}",
                            ssim_score=ssim_score  # Pass SSIM score for Moondream selection
                        )

                else:
                    raise ServiceValidationError(
                        f"File {video_path} does not exist")
            except Exception as e:
                raise ServiceValidationError(f"Error: {e}")

        # Clean up tmp dirs
        try:
            await self.hass.loop.run_in_executor(None, shutil.rmtree, tmp_clips_dir)
            _LOGGER.info(
                f"Deleted tmp folder: {tmp_clips_dir}")
        except FileNotFoundError as e:
            _LOGGER.info(f"Failed to delete tmp folder: {e}")
        try:
            await self.hass.loop.run_in_executor(None, shutil.rmtree, tmp_frames_dir)
            _LOGGER.info(
                f"Deleted tmp folder: {tmp_frames_dir}")
        except FileNotFoundError as e:
            _LOGGER.info(f"Failed to delete tmp folders: {e}")
        return self.client

    async def add_streams(self, image_entities, duration, max_frames, target_width, include_filename, expose_images):
        if image_entities:
            await self.record(
                image_entities=image_entities,
                duration=duration,
                max_frames=max_frames,
                target_width=target_width,
                include_filename=include_filename,
                expose_images=expose_images,
            )
        return self.client

    async def add_visual_data(self, image_entities, image_paths, target_width, include_filename, expose_images):
        """Wrapper for add_images for visual data"""
        await self.add_images(
            image_entities=image_entities,
            image_paths=image_paths,
            target_width=target_width,
            include_filename=include_filename,
            expose_images=expose_images
        )
        return self.client
