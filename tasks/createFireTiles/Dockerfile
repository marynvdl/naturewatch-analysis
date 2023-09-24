# Use an existing docker image as a base
FROM osgeo/gdal:ubuntu-small-latest

# Update the package list
RUN apt-get update -y

# Install runtime dependencies
RUN apt-get install -y curl gnupg

# Add Google Cloud SDK distribution URI as a package source
RUN echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list

# Import the Google Cloud public key
RUN curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key --keyring /usr/share/keyrings/cloud.google.gpg add -

# Update the package list and install the Google Cloud SDK
RUN apt-get update -y && apt-get install -y google-cloud-sdk

# Set the working directory
WORKDIR /app

# Copy the current directory contents into the container
COPY . .

# Make the shell script executable
RUN chmod +x createFireTiles.sh

# Run the script
CMD ["./createFireTiles.sh"]