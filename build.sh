docker build \
  --build-arg DSH_VERSION=dsh-v0.2.1-rc.2 \
  -t dsh-web:0.2.1-rc.2 .

docker run --rm -it \
  -p 3080:3080 \
  -v dsh-home:/home/node/.dsh \
  -e DEEPSEEK_API_KEY \
  dsh-web:0.2.1-rc.2