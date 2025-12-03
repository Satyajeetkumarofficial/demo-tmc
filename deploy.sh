#!/bin/bash
IMAGE=yourrepo/blaze-thumb-bot:latest
docker build -t $IMAGE .
docker push $IMAGE
# Then deploy on Koyeb from image or update service there.
