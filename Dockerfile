FROM python:3.8.5-buster

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y wget bzip2
RUN wget http://download.geofabrik.de/europe/germany/niedersachsen-latest.osm.bz2
RUN bunzip2 niedersachsen-latest.osm.bz2

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN pip install .
RUN statsimi model --model_out classify.mod --train niedersachsen-latest.osm

CMD statsimi http --model classify.mod --http_port 2000