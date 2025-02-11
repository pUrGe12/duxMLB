from ultralytics import YOLO

import requests
import os
from tqdm import tqdm
import zipfile
import io
from typing import Optional, Union, List, Tuple, Dict
import shutil
import cv2

import numpy as np
from dataclasses import dataclass
from collections import defaultdict

from scipy.interpolate import UnivariateSpline

import os
os.environ['QT_QPA_PLATFORM'] = 'xcb'

import matplotlib
matplotlib.use('Agg')  # Use non-GUI backend

import matplotlib.pyplot as plt

class DeblurProcessor:
    def __init__(self, num_iterations=30):
        self.num_iterations = num_iterations
        
    def estimate_psf(self, image_size=15):
        """Generate estimated Point Spread Function for deblurring"""
        center = (image_size - 1) / 2
        psf = np.zeros((image_size, image_size))
        
        # Create a motion blur kernel
        for i in range(image_size):
            for j in range(image_size):
                dist = np.sqrt((i - center) ** 2 + (j - center) ** 2)
                if dist <= center:
                    psf[i, j] = 1
        
        psf /= psf.sum()  # Normalize
        return psf
    
    def richardson_lucy(self, image: np.ndarray, psf: np.ndarray) -> np.ndarray:
        """
        Apply Richardson-Lucy deconvolution algorithm
        
        Args:
            image: Input image channel (2D array)
            psf: Point spread function kernel
            
        Returns:
            Deblurred image channel
        """
        # Initialize the estimate with the input image
        estimate = np.full(image.shape, 0.5)
        psf_flip = np.flip(np.flip(psf, 0), 1)  # Flipped PSF for convolution
        
        # Richardson-Lucy iterations
        for _ in range(self.num_iterations):
            # Compute the ratio between the input image and the estimate convolved with the PSF
            conv = cv2.filter2D(estimate, -1, psf)
            conv[conv == 0] = np.finfo(float).eps  # Avoid division by zero
            ratio = image / conv
            
            # Update the estimate
            estimate *= cv2.filter2D(ratio, -1, psf_flip)
            
        return estimate
    
    def deblur_frame(self, frame):
        """Apply Richardson-Lucy deconvolution to a frame"""
        # Convert to float32 for processing
        frame_float = frame.astype(np.float32) / 255.0
        
        # Process each channel separately
        deblurred_channels = []
        psf = self.estimate_psf()
        
        for channel in cv2.split(frame_float):
            deblurred = self.richardson_lucy(channel, psf)
            deblurred = cv2.normalize(deblurred, None, 0, 1, cv2.NORM_MINMAX)
            deblurred_channels.append(deblurred)
        
        # Merge channels and convert back to uint8
        deblurred_frame = cv2.merge(deblurred_channels)
        deblurred_frame = (deblurred_frame * 255).clip(0, 255).astype(np.uint8)
        
        return deblurred_frame


class LoadTools:
    def __init__(self):
        self.session = requests.Session()
        self.chunk_size = 1024
        self.BDL_MODEL_API = "https://balldatalab.com/api/models/"
        self.BDL_DATASET_API = "https://balldatalab.com/api/datasets/"
        self.yolo_model_aliases = {
            'bat_tracking': '../big_weights/bat_tracking.txt',
            }

    def _download_files(self, url: str, dest: Union[str, os.PathLike], is_folder: bool = False, is_labeled: bool = False) -> None:
        response = self.session.get(url, stream=True)
        if response.status_code == 200:
            total_size = int(response.headers.get('content-length', 0))
            progress_bar = tqdm(total=total_size, unit='iB', unit_scale=True, desc=f"Downloading {os.path.basename(dest)}")
            
            with open(dest, 'wb') as file:
                for data in response.iter_content(chunk_size=self.chunk_size):
                    size = file.write(data)
                    progress_bar.update(size)
            
            progress_bar.close()
            print(f"Model downloaded to {dest}")
        else:
            print(f"Download failed. STATUS: {response.status_code}")

    def _get_url(self, alias: str, txt_path: str, use_bdl_api: bool, api_endpoint: str) -> str:
        if use_bdl_api:
            return f"{api_endpoint}{alias}"
        else:
            with open(txt_path, 'r') as file:
                return file.read().strip()

    def load_model(self, model_alias: str, model_type: str = 'YOLO', use_bdl_api: Optional[bool] = True, model_txt_path: Optional[str] = None) -> str:
        model_txt_path = self.yolo_model_aliases.get(model_alias) if use_bdl_api else model_txt_path
        
        if not model_txt_path:
            raise ValueError(f"Invalid alias: {model_alias}")

        base_dir = os.path.dirname(model_txt_path)
        base_name = os.path.splitext(os.path.basename(model_txt_path))[0]

        model_weights_path = f"{base_dir}/{base_name}.pt"

        if os.path.exists(model_weights_path):
            print(f"Model found at {model_weights_path}")
            return model_weights_path

        url = self._get_url(model_alias, model_txt_path, use_bdl_api, self.BDL_MODEL_API)
        self._download_files(url, model_weights_path, is_folder=False)
        
        return model_weights_path


@dataclass
class BatDetection:
    frame_number: int
    timestamp: float
    box_coords: np.ndarray          # Coordinates of the bounding box
    confidence: float
    track_id: int = None            # Default to None

class BatTracker:
    def __init__(
        self, 
        model=YOLO, 
        min_confidence = 0.1,                   # To get more detections, we can lower this (cause its unlikely that the model will find something BAT like)
        deblur_iterations=30
        ):                                       # Since, we're using splines
        self.model = model
        self.min_confidence = min_confidence
        self.deblur_processor = DeblurProcessor(num_iterations=deblur_iterations)

    def _get_box_center(self, box_coords: np.ndarray) -> Tuple[float, float]:
        '''Calculates the center of the bounding box'''
        x1, y1, x2, y2 = box_coords[0]
        return ((x1 + x2) / 2, (y1 + y2) / 2)

    def _get_average_correction_factor(self, list_of_detections: List[BatDetection]) -> Tuple[float, float]:
        average_bat_length_in_ft = 33*0.0833333                           # This is in feet

        ratio_list = []
        for detection in list_of_detections:
            x1, y1, x2, y2 = detection.box_coords[0]
            # These are the box corners
            pixel_length = float(np.sqrt((x1-x2)**2 + (y1-y2)**2))

            ratio_pixel_to_feet = float(pixel_length / average_bat_length_in_ft)
            ratio_list.append(ratio_pixel_to_feet)
            print('added')

        average_ratio_pixel_to_feet = sum(ratio_list) / len(ratio_list)

        return average_ratio_pixel_to_feet

    # def _plot_trajectories(self, x_positions, y_positions):
    #     plt.figure(figsize=(10, 6))
    #     plt.plot(x_positions, y_positions[::-1], 'o-', label="Raw Detections", color="blue", alpha=0.7)
    #     plt.xlabel("X Position (ft)")
    #     plt.ylabel("Y Position (ft)")
    #     plt.title("Plotting the trajectories")
    #     plt.gca().invert_yaxis()
    #     plt.legend()
    #     plt.grid()
    #     plt.savefig("./figures/bat_trajectory_4_deblurred.png")


    # def _plot_splines(self, x_positions, y_positions, x_spline, y_spline, time_x):
    #     t_fine = np.linspace(time_x[0], time_x[-1], 1000)  # Fine-grained time for smooth spline
    #     x_fine = x_spline(t_fine)
    #     y_fine = y_spline(t_fine)

    #     plt.figure(figsize=(10, 6))
    #     plt.plot(x_positions, y_positions, 'o', label="Raw Detections", color="blue", alpha=0.7)
    #     plt.plot(x_fine, y_fine, '-', label="Spline Fit", color="red", linewidth=2)
    #     plt.xlabel("X Position (ft)")
    #     plt.ylabel("Y Position (ft)")
    #     plt.title("Trajectory with Spline Fit")
    #     plt.gca().invert_yaxis()
    #     plt.legend()
    #     plt.grid()
    #     plt.savefig("./figures/spline_interpolated_3_deblurred.png")


    def _calculate_splines(self, list_of_detections: List[BatDetection], frame_rate, correction_factor):
        '''
        Here we take as input the list of all the detections and calculate the x spline and y spline from it. This directly returns the x_spline and y_spline.

        You're basically supposed to call this function once you have finished detecting for all frames and stored it in the relevant array.
        '''

        x_positions = []
        y_positions = []

        for detection in list_of_detections:
            x, y = self._get_box_center(detection.box_coords)
            # print(f"{x}, {y} are the coordinates of the center in this frame (pixels) -- Bat's center of gravity")
            # print(f"{x/correction_factor }, {y/correction_factor} are the coordinates of the center in this frame (ft) -- Bat's center of gravity")

            # Since we are taking a differential it shouldn't matter at all
            x_positions.append(x/correction_factor)
            y_positions.append(y/correction_factor)

        # print('plotting trajectories')
        # self._plot_trajectories(x_positions, y_positions)
        
        # Should be the same...
        time_x = np.linspace(0, len(x_positions) / frame_rate, len(x_positions))              
        time_y = np.linspace(0, len(y_positions) / frame_rate, len(y_positions))
        # print(f'this is time x: {time_x}')
        # print(f'this is time y: {time_y}')

        x_spline = UnivariateSpline(time_x, x_positions, s = 1)             # Setting a smoothing factor to ensure that we don't fit the data exactly (as the data is noisy!)
        y_spline = UnivariateSpline(time_y, y_positions, s = 1)

        # print('plotting spines')
        # self._plot_splines(x_positions, y_positions, x_spline, y_spline, time_x)

        return (x_spline, y_spline, time_x, time_y)


    # def _plot_speed(self, speed, t_fine):
    #     """
    #     The objective is to look out for:

    #     1. Smooth speed changes are expected
    #     2. Large spikes may indicate noise in the trajectory.
    #     """

    #     plt.figure(figsize=(10, 6))
    #     plt.plot(t_fine, speed, label="Speed (ft/s)", color="green")
    #     plt.xlabel("Time (s)")
    #     plt.ylabel("Speed (ft/s)")
    #     plt.title("Speed Over Time")
    #     plt.legend()
    #     plt.grid()
    #     plt.savefig("./figures/speed_plot_3_deblurred.png")


    def _calculate_speed(self, splines_tuple: Tuple[UnivariateSpline, UnivariateSpline], time):
        '''
        Takes the two splines wrt time and from those calculates their time derivates to find the fastest speed
        Here I am using a finer time array (now we can cause we basically have a function mapping from time to x and y coordinates) to take finer derivatives
        
        Returns the maximum speed from the speed array
        '''
        x_spline, y_spline = splines_tuple              # decompose it. These splines are based on ft calculations itself

        t_fine = np.linspace(time[0], time[-1], 1000)  # Fine-grained time array for better resolution
        dx_dt = x_spline.derivative()(t_fine)
        dy_dt = y_spline.derivative()(t_fine)

        speed = np.sqrt(dx_dt**2 + dy_dt**2)                # This is an array of ft speed itself
        
        # print('plotting speeds')
        # self._plot_speed(speed, t_fine)

        return np.max(speed)


    def process_video(self, video_path: str) -> Dict:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        all_detections: List[BatDetection] = []
        frame_number = 0

        while cap.isOpened():
            success, frame = cap.read()
            if not success:
                break
                
            timestamp = frame_number / fps
            print(f"This is the timestamp: {timestamp}")

            # Apply deblurring before detection
            deblurred_frame = self.deblur_processor.deblur_frame(frame)
            
            # Save original and deblurred frames for comparison (optional)
            if frame_number == 0:  # Save first frame as example
                cv2.imwrite('original_frame.jpg', frame)
                cv2.imwrite('deblurred_frame.jpg', deblurred_frame)
            
            # Get detections for current frame using deblurred image
            results = self.model.predict(deblurred_frame, verbose=False)
            print(f"detecting for frame {frame_number}")

            for r in results:
                for box in r.boxes.cpu().numpy():
                    confidence = float(box.conf)
                    if confidence >= self.min_confidence:
                        detection = BatDetection(
                            frame_number=frame_number,
                            timestamp=timestamp,
                            box_coords=box.xyxy,
                            confidence=confidence,
                            track_id=int(box.id) if box.id is not None else None
                        )
                        all_detections.append(detection)

            frame_number += 1

        cap.release()

        average_correction_factor = self._get_average_correction_factor(all_detections)
        x_spline, y_spline, time_x, time_y = self._calculate_splines(all_detections, fps, average_correction_factor)
        max_speed = self._calculate_speed((x_spline, y_spline), time_x)

        return max_speed

'''
The basic idea is as follows:

1. Detect the bat in every frame possible.
2. Store the coordinates of the center of the bounding box (and hence the bat) in a list or something
3. Calculate a cubic spline function based on those values for y(t) and x(t) where t is the time
4. Differentiate wrt to time and find the maximum velocity
'''

# if __name__ == "__main__":
#     SOURCE_VIDEO_PATH = "./input/baseball_2.mp4"

#     load_tools = LoadTools()
#     model_weights = load_tools.load_model(model_alias='bat_tracking')
#     model = YOLO(model_weights)

#     tracker = BatTracker(
#         model = model,
#         min_confidence = 0.2,
#         deblur_iterations=30
#     )

#     results = tracker.process_video(SOURCE_VIDEO_PATH)

#     print(f'max bat swing speed in ft per second was: {results}')