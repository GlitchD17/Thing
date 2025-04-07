from flask import Flask
from flask import request, jsonify, url_for,render_template
import requests
import gzip
import random
import string
from warcio.archiveiterator import ArchiveIterator
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import os
import mimetypes
import threading # Still using threading for simplicity in this example
from PIL import Image as PILImage # Using Pillow for image validation

# --- Configuration ---
# Directory for storing downloaded HTML files (not directly served)
HTML_SAVE_DIR = os.path.join(os.path.dirname(__file__), 'data', 'html')
# Directory for storing generated images (served by Flask)
IMAGE_SAVE_DIR = os.path.join(os.path.dirname(__file__), 'static', 'generated_images')
# Ensure directories exist
os.makedirs(HTML_SAVE_DIR, exist_ok=True)
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)

# Common Crawl configuration
DEFAULT_CRAWL = 'CC-MAIN-2025-13' # Using a slightly older, more likely stable index
HEADERS = {'User-Agent': 'MyWebAppImageGenerator/1.0'}
MAX_WARC_TRIES = 100000
MAX_RECORDS_TO_CHECK = 100000
MAX_IMAGES_TO_TRY = 5

# --- Flask App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_very_secret_key_here' # Important for session management etc. if needed later

# --- Helper Functions ---
def generate_random_tag():
    """Generates a random string tag for filenames."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

def validate_image(image_path):
    """Checks if an image file is valid using Pillow."""
    try:
        img = PILImage.open(image_path)
        img.verify()  # Verify headers
        # Re-open to actually load data, verify() consumes the file handle
        img = PILImage.open(image_path)
        img.load() # Try to load the image data
        return True
    except Exception as e:
        print(f"Image validation failed for {image_path}: {e}")
        return False

# --- Core Image Generation Logic ---
def find_random_image():
    """
    Attempts to find, download, validate, and save a random image from Common Crawl.
    Returns the relative path (for URL generation) of the saved image if successful,
    otherwise returns None.
    """
    crawl_index = request.args.get('crawl', DEFAULT_CRAWL) # Allow specifying crawl via query param if needed

    for warc_try in range(MAX_WARC_TRIES):
        print(f"Attempting WARC file {warc_try + 1}/{MAX_WARC_TRIES}...")
        try:
            # Fetch WARC paths list
            warc_paths_url = f'https://data.commoncrawl.org/crawl-data/{crawl_index}/warc.paths.gz'
            response = requests.get(warc_paths_url, headers=HEADERS, timeout=20)
            response.raise_for_status() # Raise an exception for bad status codes
            warc_paths = gzip.decompress(response.content).decode().splitlines()
            if not warc_paths:
                print(f"No WARC paths found for crawl index {crawl_index}")
                continue

            selected_warc = random.choice(warc_paths)
            print(f"Selected WARC: {selected_warc}")

            # Set up S3 client for anonymous access
            s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
            bucket = 'commoncrawl'
            stream = None

            # Retrieve the WARC file
            try:
                s3_response = s3.get_object(Bucket=bucket, Key=selected_warc)
                stream = s3_response['Body']
                print("Streaming from S3...")
            except ClientError as e:
                print(f"S3 access failed ({e}), falling back to HTTP.")
                warc_url = f"https://data.commoncrawl.org/{selected_warc}"
                http_response = requests.get(warc_url, headers=HEADERS, stream=True, timeout=60)
                http_response.raise_for_status()
                http_response.raw.decode_content = True
                stream = http_response.raw
                print("Streaming from HTTP...")

            if not stream:
                print("Failed to get stream for WARC file.")
                continue

            # Process the WARC file
            records_checked = 0
            for record in ArchiveIterator(stream):
                if record.rec_type == 'response':
                    records_checked += 1
                    if records_checked > MAX_RECORDS_TO_CHECK:
                        print(f"Checked {MAX_RECORDS_TO_CHECK} records, moving to next WARC.")
                        break # Move to the next WARC file

                    content_type = record.http_headers.get_header('Content-Type', '')
                    if 'html' not in content_type:
                        continue # Skip non-html responses

                    url = record.rec_headers.get_header('WARC-Target-URI')
                    if not url: continue

                    print(f"Processing HTML from: {url}")

                    # Read content safely
                    try:
                        content = record.content_stream().read()
                    except Exception as e:
                        print(f"Error reading record content: {e}")
                        continue

                    # Save HTML (optional, kept for parity with original request)
                    try:
                        domain = urlparse(url).netloc or 'unknown_domain'
                        # Sanitize domain name for filename
                        safe_domain = "".join(c if c.isalnum() or c in ['.', '-'] else '_' for c in domain)
                        html_filename = f"{safe_domain}_{generate_random_tag()}.html"
                        html_path = os.path.join(HTML_SAVE_DIR, html_filename)
                        with open(html_path, 'wb') as f:
                            f.write(content)
                        # print(f"HTML saved to {html_path}") # Less verbose logging
                    except Exception as e:
                        print(f"Error saving HTML: {e}")
                        # Continue processing even if HTML saving fails

                    # Extract and shuffle image URLs
                    try:
                        soup = BeautifulSoup(content, 'html.parser')
                        img_tags = soup.find_all('img')
                        image_urls = []
                        for img in img_tags:
                            src = img.get('src')
                            if src:
                                try:
                                     # Handle relative URLs and ensure http(s) scheme
                                     abs_url = urljoin(url, src)
                                     if urlparse(abs_url).scheme in ['http', 'https']:
                                         image_urls.append(abs_url)
                                except ValueError:
                                    print(f"Skipping invalid src: {src}")


                        random.shuffle(image_urls)
                    except Exception as e:
                        print(f"Error parsing HTML or extracting images: {e}")
                        continue # Skip this record if parsing fails

                    # Try downloading and validating an image
                    images_attempted_in_record = 0
                    for img_url in image_urls:
                        if images_attempted_in_record >= MAX_IMAGES_TO_TRY:
                             break # Limit attempts per HTML page

                        images_attempted_in_record += 1
                        print(f"Attempting image download: {img_url}")
                        try:
                            img_response = requests.get(img_url, headers=HEADERS, timeout=10, stream=True)
                            img_response.raise_for_status()

                            content_type = img_response.headers.get('Content-Type', '')
                            if not content_type.startswith('image/'):
                                print(f"Skipping non-image content type: {content_type}")
                                continue

                            # Generate filename and save path
                            extension = mimetypes.guess_extension(content_type) or '.jpg'
                            # Ensure extension starts with a dot
                            if not extension.startswith('.'):
                                extension = '.' + extension
                            # Limit extension length and characters
                            extension = ''.join(c for c in extension if c.isalnum() or c == '.')[:5]
                            if not extension or len(extension) < 2: # Basic check for valid extension
                                extension = '.jpg'

                            random_tag = generate_random_tag()
                            filename = f"{random_tag}{extension}"
                            img_path = os.path.join(IMAGE_SAVE_DIR, filename)

                            # Download image content
                            with open(img_path, 'wb') as f:
                                for chunk in img_response.iter_content(chunk_size=8192):
                                    f.write(chunk)

                            # Validate the downloaded image
                            if validate_image(img_path):
                                print(f"Successfully downloaded and validated image: {img_path}")
                                stream.close() # Close the WARC stream
                                # Return path relative to 'static' directory
                                return os.path.join('generated_images', filename)
                            else:
                                print(f"Invalid image downloaded: {img_url}. Deleting.")
                                os.remove(img_path) # Clean up invalid image file
                                continue # Try the next image URL

                        except requests.exceptions.RequestException as e:
                            print(f"Failed to download image {img_url}: {e}")
                            # Clean up potentially partially downloaded file if it exists
                            if 'img_path' in locals() and os.path.exists(img_path):
                                try: os.remove(img_path)
                                except OSError: pass
                            continue # Try the next image URL
                        except Exception as e:
                            print(f"An unexpected error occurred processing image {img_url}: {e}")
                            if 'img_path' in locals() and os.path.exists(img_path):
                                try: os.remove(img_path)
                                except OSError: pass
                            continue # Try the next image URL

            # Close the stream if loop finishes without finding image
            if stream:
                stream.close()

        except requests.exceptions.RequestException as e:
            print(f"Network error accessing Common Crawl data for WARC try {warc_try + 1}: {e}")
        except boto3.exceptions.Boto3Error as e:
             print(f"AWS S3 error for WARC try {warc_try + 1}: {e}")
        except Exception as e:
            print(f"An unexpected error occurred during WARC processing (Try {warc_try + 1}): {e}")
            # Ensure stream is closed if open
            if 'stream' in locals() and stream and hasattr(stream, 'close'):
                 try: stream.close()
                 except Exception: pass

    # If no image is found after all attempts
    print("Failed to find a suitable image after all attempts.")
    return None

# --- Flask Routes ---
@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template('index.html')

@app.route('/generate-image', methods=['POST'])
def generate_image_route():
    """Handles the request to generate a new image."""
    # In a real app, use a task queue here.
    # For simplicity, we run it directly, blocking the request.
    image_rel_path = find_random_image()

    if image_rel_path:
        # Generate the URL for the image path relative to 'static'
        image_url = url_for('static', filename=image_rel_path, _external=True) #_external=True gives absolute URL
        return jsonify({'success': True, 'image_url': image_url})
    else:
        return jsonify({'success': False, 'message': 'Failed to find a suitable image after multiple attempts.'})

# --- Main Execution ---
if __name__ == '__main__':
    # Use host='0.0.0.0' to make it accessible on your network
    # Debug=True is helpful during development but should be OFF in production
    app.run(host='0.0.0.0', port=5000, debug=False)
