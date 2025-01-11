from flask import Flask, request, jsonify
from flask_cors import CORS

import os
import sys

from moviepy.editor import VideoFileClip

# imports for scraping and downloading youtube videos for old classical matches
import os
from yt_dlp import YoutubeDL

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import time

# For getting the video's link using selenium
# Note that in the future, this is the most likely part that will get fucked

# MAKE SURE THAT YOU CHECK THESE VALUES BEFORE SUBMISSION, that is, the values of the CSS selectors
def get_url(user_query):

    chrome_options = Options()
    chrome_options.add_argument("--headless")  # Run in headless mode
    chrome_options.add_argument("--disable-gpu")  # Disable GPU acceleration
    chrome_options.add_argument("--no-sandbox")  # Bypass OS security model
    chrome_options.add_argument("--disable-dev-shm-usage")  # Overcome limited resource problems
    chrome_options.add_argument("--window-size=1920x1080")  # Set window size for proper element rendering

    driver = webdriver.Chrome(options=chrome_options)

    driver.get(f'https://www.youtube.com/results?search_query={user_query}')

    # WHY CANT I CLICK HERE? This is for filtering based on 4-20 minutes. Commenting this for now
    # filter_for_four_to_twenty_minutes = driver.find_element(By.CSS_SELECTOR, "yt-chip-cloud-chip-renderer.style-scope:nth-child(9) > div:nth-child(2)").click()

    time.sleep(1)           # let the page load

    # This will always find the first link of the video (it won't pick up the sponsor messages so chill)
    link = driver.find_element(By.XPATH, "/html/body/ytd-app/div[1]/ytd-page-manager/ytd-search/div[1]/ytd-two-column-search-results-renderer/div/ytd-section-list-renderer/div[2]/ytd-item-section-renderer/div[3]/ytd-video-renderer[1]/div[1]/div/div[1]/div/h3/a").get_attribute('href')

    # link has currently the & part which is something we don't care about
    link = str(link.split('&')[0])

    return link

# Making post requests to the getBuffer method
import requests             

sys.path.append(os.path.dirname(os.path.abspath(__file__))) # adding the root directory to path
# This should be done before any relative imports, adding the "backend" directory as root

# Imports for API querying 
from API_querying.query import call_API, pretty_print, figure_out_code
from API_querying.query import team_code_mapping, player_code_mapping

# Imports for helpers
from models.helper_models import check_buffer_needed, is_it_gen_stuff
from models.helper_models import gen_talk

"""
This is the backend file for the options page. This is supposed to be the MLB future predictor. The way this is done is as follows

Step 1 --> Extract the user's stats based on their prompt and give it numerical values. 
Step 2 --> Create a query of those stats and send it to pinecone.
Step 3 --> Access the top players from pinecone who match the user's performance.
Step 4 --> Based on additional information (if provided by the user) further single out 2 players from the top few.
Step 5 --> Wrap it around Gemini's response and push it to the user as a statistical estimate of how well their future in MLB may turn out to be based on their current performance.
"""

import numpy as np
import pandas as pd

from google.generativeai.types import HarmCategory, HarmBlockThreshold
import google.generativeai as genai

from flask import Flask, request, jsonify
from flask_cors import CORS

from prompts import statPrompt

from dotenv import load_dotenv                # Don't need to do this if we've specified .env contents in render
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / '.env')

API_KEY = str(os.getenv("API_KEY")).strip()

# print(API_KEY)
pinecone_api_key = str(os.getenv("pinecone_api_key")).strip()

genai.configure(api_key=API_KEY)

model = genai.GenerativeModel('gemini-pro')
chat = model.start_chat(history=[])

# ---------------------------------------------------------------------------
# Extraction model calling and implementation
# ---------------------------------------------------------------------------
from models.extraction import extractor

# ---------------------------------------------------------------------------
# This is the code for loading the trained models, and querying the vectorDB
# ---------------------------------------------------------------------------

from tensorflow.keras.models import load_model, Model
import joblib
from pinecone.grpc import PineconeGRPC as Pinecone
from pinecone import ServerlessSpec

def load_models():
    '''
        The encoder and the scaler have been trained through the notebook present in the prediction directory
        The datasets used to train these are present inside the datasets directory. These are exactly what were provided by the organisers.
    
        input: None
        return: instances of the encoder and the scaler model.
    '''

    encoder = load_model('../prediction/models/encoder_model.h5')
    scaler = joblib.load('../prediction/models/scaler.joblib')
    
    return encoder, scaler


global encoder, scaler
encoder, scaler = load_models()             # Load the encoder and the scaler using the above defined function

def process_new_hit(new_hit_data, encoder, scaler):
    """
        This function takes in the players stats and generates embeddings using the trained models
    """
    
    assert type(new_hit_data) == dict, 'The hit data must be a dictionary'
    features = np.array([[
        new_hit_data['ExitVelocity'],
        new_hit_data['HitDistance'],
        new_hit_data['LaunchAngle']
    ]])
    
    embedding = encoder.predict(scaler.transform(features))
    return embedding[0]

def find_similar_hits(embedding, index_name="baseball-hits", top_k=5):
    """Find similar hits in the database"""

    pinecone = Pinecone(api_key=pinecone_api_key)
    
    index = pinecone.Index(index_name)
    results = index.query(
        vector=embedding.tolist(),
        top_k=top_k,
        include_metadata=True
    )
    return results

def store_similar_hits(results):
    """Store the details of similar hits in a structured format."""

    matches = []
    for idx, match in enumerate(results.matches, 1):
        match_details = {
            "SimilarityScore": round(match.score, 3),
            "Title": match.metadata.get("title", "Unknown"),
            "ExitVelocity": f"{float(match.metadata.get('exit_velocity', 0.0)):.1f} mph",
            "HitDistance": f"{float(match.metadata.get('hit_distance', 0.0)):.1f} feet",
            "LaunchAngle": f"{float(match.metadata.get('launch_angle', 0.0)):.1f} degrees",
        }
        matches.append(match_details)
    
    return matches

# ---------------------------------------------------------------------------
# Model to wrap things and output final answer
# ---------------------------------------------------------------------------

def GPT_response(top_similar_hits, additional_params):
    prompt = str(statPrompt[0]) + f"""
                                These are the top similar homeruns in MLB: {top_similar_hits}
                                These are the additional user stats: {additional_params}
        """                         
    
    try:
        output = ''
        response = chat.send_message(prompt, stream=False, safety_settings={
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE, 
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE
        })

        # Sometimes the model acts up...
        if not response:
            raise ValueError("No response received")

        for chunk in response:
            if chunk.text:
                output += str(chunk.text)

        return output

    except Exception as e:
        print(f"Error generating response: {e}")
        return 'Try again'

# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#                                                                              Code for generating baseball speed
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

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

from baseball_detect.flow import LoadTools, BallDetection, BaseballTracker          # This will handle everything

def convert_webm_to_mp4(input_path, output_path):
    clip = VideoFileClip(input_path)
    clip.write_videofile(output_path, codec="libx264")
    clip.close()

# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
#                                                                                       API endpoints
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes


# Upload configs for classics-video
UPLOAD_FOLDER = 'uploads/videos'                                    # To save the uploaded videos
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/process_input', methods=['POST'])
def process_input():
    user_input = request.json.get('input')  # Receive input from the panel
    if not user_input:
        return jsonify({"error": "No input provided"}), 400

    '''
    Here is how the processing takes place
    1. Figure out if the buffer is required. 
    2. If it is not required, then answer general baseball stats. This is what panel_response is
    3. If it is required, then process the video and answer.

    We'll focus on figuring out 2 statcast datas from the videos, those are the pitcher's speed, and the batsman's hit velocities
    '''
    
    boolean = check_buffer_needed(user_input)
    
    if boolean == False:             # That is no buffer needed
        # print('buffer not needed!')
        
        '''
        Here, we may have to call the APIs
        The idea here is to parse the input, determine if its general baseball stuff or particular to a player, or a team or schedule, in which case we'll call the API querying function.
        '''

        if is_it_gen_stuff(user_input):
            print('Requires APIs')

            name_code_tuple = figure_out_code(team_code_mapping, player_code_mapping, user_input)
            output = call_API(name_code_tuple)

            # processed_output = output
            # print(output)

            processed_output = pretty_print(output)             # not doing this if the output is too big as in the case of the schedule
        
        else:
            processed_output = gen_talk(user_input)          # We'll add a normal response generator here

        return jsonify({"response": processed_output})


    else:            # that is buffer is required
        print('buffer is needed!')

        # Make a post request to the extension with the action getBuffer. (I am worried about the syntax here)
        response = requests.post(
            f"http://127.0.0.1:5000/extensions/{chrome_extension_id}/getBuffer",                    # Hope we're actually getting the buffer
            json={'action': "getBuffer"}
        )
        if response.status_code == 200:
            video_data = response.content
            with open("./input_files/received_video.webm", "wb") as fp:               # Saving this in the input files directory (hopefully this works)
                fp.write(video_data)

            # Convert webm to mp4 and save it
            convert_webm_to_mp4("./input_files/received_video.webm", "./input_files/converted_input.mp4")
            
            VideoPath = "./input_files/converted_input.mp4"


            # -------------------------------------------------------------------------------------------
            #               Now we need a logic to see if they're asking for speed or what
            # -------------------------------------------------------------------------------------------

            what_is_needed = check_statcast(user_input)

            if 'baseballspeed' in what_is_needed.strip().lower():
                
                # now we gotta process this, assuming we get the data as a mp4 data
                load_tools = LoadTools()
                model_weights = load_tools.load_model(model_alias='ball_trackingv4')
                model = YOLO(model_weights)

                tracker = BaseballTracker(
                model=model,
                min_confidence=0.3,         # 0.3 confidence works good enough, gives realistic predictions
                max_displacement=100,       # adjust based on your video resolution
                min_sequence_length=7,
                pitch_distance_range=(50, 70)  # feet
                )
            
                # Process video
                results = tracker.process_video(VideoPath)                                  # This should probably work
                
                # Printing and saving results
                output = """"""

                print(f"\nProcessed {results['total_frames']} frames at {results['fps']} FPS")
                print(f"Found {len(results['sequences'])} valid ball sequences")
                
                output += f"\nProcessed {results['total_frames']} frames at {results['fps']} FPS" + f"\n Found {len(results['sequences'])} valid ball sequences"

                for i, speed_est in enumerate(results['speed_estimates'], 1):
                    print(f"\nSequence {i}:")
                    print(f"Frames: {speed_est['start_frame']} to {speed_est['end_frame']}")
                    print(f"Duration: {speed_est['time_duration']:.2f} seconds")
                    print(f"Average confidence: {speed_est['average_confidence']:.3f}")
                    print(f"Estimated speed: {speed_est['min_speed_mph']:.1f} to "
                          f"{speed_est['max_speed_mph']:.1f} mph")

                    output += f"""
        \nSequence {i}:
        Frames: {speed_est['start_frame']} to {speed_est['end_frame']}
        Duration: {speed_est['time_duration']:.2f} seconds
        Average confidence: {speed_est['average_confidence']:.3f}
        Estimated speed: {speed_est['min_speed_mph']:.1f}""" + f""" to {speed_est['max_speed_mph']:.1f} mph
                    """

                return jsonify({"response": output}), 200
            
            elif 'exitvelocity' in what_is_needed.strip().lower():
                # This now requires tracking the bat.
                pass

            else:
                return jsonify({"error": "Failed to get buffer"}), 500




@app.route('/user-stat/', methods=['POST'])
def user_stat():
    user_input = request.json.get('input')  # Receive input from the panel

    print(user_input)

    if not user_input:
        return jsonify({"error": "No input provided"}), 400

    try:
        print('in the try block')
        extractor_dictionary, additional_params = extractor(user_input)

        print("unpacked the tuple!")

        '''  
            extractor output is a dictionary that describes the following of the user
            1. ExitVelocity
            2. HitDistance
            3. LaunchAngle

            We parse the "AdditionalParams" seperately. The idea is to send the extractor_dictionary to the pineconeDB and get top similar hits

        '''

        embedding = process_new_hit(extractor_dictionary, encoder, scaler)                           
        
        print("embeddings generated")

        # Find the top 5 matches to the user's stats being entered above.
        found_similar_hits = find_similar_hits(embedding, top_k = 5)
        top_similar_hits = store_similar_hits(found_similar_hits)               # Storing that to send it to the model for prettifying

        print(top_similar_hits)

        processed_output = GPT_response(top_similar_hits, additional_params.get('AdditionalParams'))                    # Both additional params and extractor dict are dictionaries.

        print(processed_output)

        return jsonify({
            "response": processed_output
            })

    except Exception as e:
        incomplete_text = extractor(user_input)

        print(incomplete_text)
        print('here in exception', e)

        '''
            Enter this block of code when unpacking the tuple leads to an exception
            This will only happen when there is only one element being returned by the extractor, that is the incomplete text. 

            We will show the incomplete text directly to the user. 
        '''

        return jsonify({
            "response": incomplete_text
            })


# -----------------------------------------------------------X Classics endpoints X--------------------------------------------------------------

from werkzeug.utils import secure_filename                              # Adding this here cause not always requireds

@app.route('/classics-video/', methods=['POST'])
def classics_video_processing():
    '''
    Video processing essentially means applying the yolo model and computing the statcast data.

    We additionally wanna make sure that the entered file is mp4 itself! Not to have a php file being executed here and my laptop becoming compromised!
    '''
    try:
        # Check if video file is in request
        if 'video' not in request.files:
            return jsonify({'error': 'No video file in request'}), 400
        
        video_file = request.files['video']                                                 # This is a mp4 video file
        
        if video_file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
            
        if not allowed_file(video_file.filename):
            return jsonify({'error': 'Invalid file type'}), 400
            
        # Secure the filename and save the file
        filename = secure_filename(video_file.filename)
        
        print(filename)

        # Ensure upload directory exists and saving it
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        video_file.save(file_path)
        
        print('here')
        # Process the video
        load_tools = LoadTools()
        model_weights = load_tools.load_model(model_alias='ball_trackingv4')
        model = YOLO(model_weights)

        tracker = BaseballTracker(
        model=model,
        min_confidence=0.3,         # 0.3 confidence works good enough, gives realistic predictions
        max_displacement=100,       # adjust based on your video resolution
        min_sequence_length=7,
        pitch_distance_range=(50, 70)  # feet
        )
        
        SOURCE_VIDEO_PATH = f'/home/purge/Desktop/MLBxG-extension/src/backend/uploads/videos/{filename}'
        print(SOURCE_VIDEO_PATH)
        # Process video
        results = tracker.process_video(SOURCE_VIDEO_PATH)
        
        # Printing and saving results
        output = """"""

        print(f"\nProcessed {results['total_frames']} frames at {results['fps']} FPS")
        print(f"Found {len(results['sequences'])} valid ball sequences")
        
        output += f"\nProcessed {results['total_frames']} frames at {results['fps']} FPS" + f"\n Found {len(results['sequences'])} valid ball sequences"

        for i, speed_est in enumerate(results['speed_estimates'], 1):
            print(f"\nSequence {i}:")
            print(f"Frames: {speed_est['start_frame']} to {speed_est['end_frame']}")
            print(f"Duration: {speed_est['time_duration']:.2f} seconds")
            print(f"Average confidence: {speed_est['average_confidence']:.3f}")
            print(f"Estimated speed: {speed_est['min_speed_mph']:.1f} to "
                  f"{speed_est['max_speed_mph']:.1f} mph")

            output += f"""
\nSequence {i}:
Frames: {speed_est['start_frame']} to {speed_est['end_frame']}
Duration: {speed_est['time_duration']:.2f} seconds
Average confidence: {speed_est['average_confidence']:.3f}
Estimated speed: {speed_est['min_speed_mph']:.1f}""" + f""" to {speed_est['max_speed_mph']:.1f} mph"""

        return jsonify({                                                            # Also need to return uploaded message
            'message': 'Video analysed successfully, now running plan',
            'filename': filename,
            'output': output                                                                # Handle this properly in the frontend
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/classics-text/', methods=['POST'])
def classics_text_processing():
    '''
    Here we will have to first scrape the video! Then apply the same concept as above
    
    With this we have downloaded the video, now we can start processing.
    Now, this video is likely going to be very long! (may even be hours long). So how do we parse this and generate meaningful output?

    1. Ensure that the video we're downloading is less than 20 minutes (so, its more like highlights rather than the entire match)
    2. If the video length is more than 20 minutes, ask the user to be generous otherwise it'll take way too much time!
    3. So, we're detecting valid sequences of continous ball movements. In highlights, this may refer to either a pitch, or a hit and travel by the ball, or a player's throw
    4. We need to see how we differentiate that. Also, we can use timestamps of the video itself to let the know the ball speeds for the sequence within that timestamp!

    So basically, the user get's an output like "within the 5th to 6th second, the ball was travelling at a speed of 97mph" and that one second was the pitch. This way. 

    Now, we can do similar things for the batsman's hit. This will be easier because the logic is then to have both the bat and the ball in the same frame, with distance between them reducing, 
    and the bat swinging. Just code these conditions and we're done.
    '''

    user_input = request.json.get('input')  # Receive input from the panel
    print(user_input)

    if not user_input:
        return jsonify({"error": "No input provided"}), 400

    # assuming user_input is a properly formatted input that we can directly use to search youtube (later we'll add LLM)

    custom_url = get_url(user_input)                                                    # based on user's input search youtube and select the first link
    path = '/home/purge/Desktop/MLBxG-extension/src/backend/downloads_classical'                # note that we can later change all these to match the path of the user

    ydl_opts = {
        'outtmpl': os.path.join(path, '%(title)s.%(ext)s'),
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([custom_url])

    return jsonify({
        'message': f"Downloaded URL: {custom_url}"
        })


@app.route('/load-yolo-model/', methods=['POST'])
def loading_yolo_models():
    load_tools = LoadTools()
    model_weights = load_tools.load_model(model_alias='ball_trackingv4')
    model = YOLO(model_weights)

    return jsonify({
        'message': "loaded deeplearning model"
        })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)

# if __name__ == "__main__":
#     port = int(os.environ.get("PORT", 10000))  # Render will provide PORT env variable
#     app.run(host="0.0.0.0", port=port)