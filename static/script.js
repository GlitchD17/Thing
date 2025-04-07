document.addEventListener('DOMContentLoaded', () => {
    const generateButton = document.getElementById('generate-button');
    const resultImage = document.getElementById('result-image');
    const statusDiv = document.getElementById('status');


    generateButton.addEventListener('click', async () => {
        // Disable button and show loading status
        generateButton.disabled = true;
        generateButton.classList.add('loading');
        statusDiv.textContent = 'Finding new image... Please wait.';
        resultImage.src = ''; // Clear previous image
        resultImage.alt = 'Generating...';


        try {
            const response = await fetch('/generate-image', {
                method: 'POST',
                 headers: {
                    // If you add CSRF protection in Flask, include the token here
                    'Content-Type': 'application/json' // Indicate we expect JSON back
                },
                // No body needed for this simple POST trigger
            });


            // Check if the response is ok (status code 200-299)
            if (!response.ok) {
                 // Try to parse error message from JSON if possible
                let errorMsg = `HTTP error! Status: ${response.status}`;
                try {
                    const errorData = await response.json();
                    errorMsg = errorData.message || errorMsg;
                } catch (e) { /* Ignore if response is not JSON */ }
                throw new Error(errorMsg);
            }


            const data = await response.json();


            if (data.success && data.image_url) {
                resultImage.src = data.image_url;
                resultImage.alt = 'Generated Image';
                statusDiv.textContent = 'Image displayed!';
            } else {
                statusDiv.textContent = data.message || 'Failed to generate image. Unknown error.';
                resultImage.alt = 'Failed to load image';
            }


        } catch (error) {
            console.error('Error fetching image:', error);
            statusDiv.textContent = `Error: ${error.message}`;
            resultImage.alt = 'Error loading image';
        } finally {
            // Re-enable button regardless of success or failure
            generateButton.disabled = false;
            generateButton.classList.remove('loading');
        }
    });
});