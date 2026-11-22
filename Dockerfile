FROM rust:1-bookworm AS native_builder
WORKDIR /build
COPY csv_cleaner.rs /build/csv_cleaner.rs
RUN rustc -O /build/csv_cleaner.rs -o /csv_cleaner

FROM python:3.12-slim-bookworm
RUN useradd --uid 1000 --no-create-home --home-dir /scratch app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY --from=native_builder /csv_cleaner /app/csv_cleaner
COPY pipeline/ /app/pipeline/
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
CMD ["/app/run.sh"]
