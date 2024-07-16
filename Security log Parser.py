import re
import logging
import signal
import json
import time
import asyncio
import psutil
import aiofiles

from aio_pika import connect_robust, Message, ExchangeType

# Setup logging
logging.basicConfig(filename='/var/log/asterisk/security-parser.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Compile the regex patterns outside of the loop for efficiency
failed_pattern = re.compile(
    r'SecurityEvent="(ChallengeResponseFailed|InvalidAccountID|FailedACL)".*?RemoteAddress="IPV4/UDP/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/\d+"'
)
success_pattern = re.compile(
    r'SecurityEvent="SuccessfulAuth".*?RemoteAddress="IPV4/UDP/(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/\d+"'
)

# Define a flag to control the main loop
running = True

# Define a signal handler to handle graceful shutdown
def signal_handler(signum, frame):
    global running
    running = False
    logging.info("Shutdown signal received. Exiting...")

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def send_to_rabbitmq(channel, message):
    send_start_time = time.time()
    try:
        exchange = await channel.declare_exchange('IPREPUTATION', ExchangeType.DIRECT, durable=True)
        await exchange.publish(
            Message(
                body=json.dumps(message).encode(),
                delivery_mode=2  # make message persistent
            ),
            routing_key='rs_queue'
        )
        logging.info(f"Sent message to RabbitMQ: {message}")
    except Exception as e:
        logging.error(f"Failed to send message to RabbitMQ: {e}")
    send_end_time = time.time()
    logging.info(f"Time taken to send message: {send_end_time - send_start_time:.6f} seconds")

async def process_log_line(line, channel):
    """Process a single log line and send relevant information to RabbitMQ."""
    parse_start_time = time.time()
    failed_match = failed_pattern.search(line)
    message = None
    if failed_match:
        _, remote_ip = failed_match.groups()
        message = {"IP": remote_ip, "result": "negative"}
    else:
        success_match = success_pattern.search(line)
        if success_match:
            remote_ip = success_match.group(1)
            message = {"IP": remote_ip, "result": "positive"}

    parse_end_time = time.time()
    logging.info(f"Time taken to parse log line: {parse_end_time - parse_start_time:.6f} seconds")

    if message:
        await send_to_rabbitmq(channel, message)

    ## BENCHMARKING ##
async def log_script_metrics():
    """Log the script's CPU, memory, disk, and network usage metrics."""
    process = psutil.Process()

    # Get initial I/O counters
    initial_disk_io = psutil.disk_io_counters()
    initial_net_io = psutil.net_io_counters()

    try:
        while running:
            # CPU usage percentage
            cpu_usage_percentage = process.cpu_percent(interval=1)

            # Memory usage
            memory_info = process.memory_info()
            memory_rss_mb = memory_info.rss / (1024 * 1024)  # Convert from bytes to MB
            memory_vms_mb = memory_info.vms / (1024 * 1024)  # Convert from bytes to MB

            # Disk usage (approximation)
            current_disk_io = psutil.disk_io_counters()
            disk_read_mb = (current_disk_io.read_bytes - initial_disk_io.read_bytes) / (1024 * 1024)
            disk_write_mb = (current_disk_io.write_bytes - initial_disk_io.write_bytes) / (1024 * 1024)
            initial_disk_io = current_disk_io

            # Network usage (approximation)
            current_net_io = psutil.net_io_counters()
            net_sent_mb = (current_net_io.bytes_sent - initial_net_io.bytes_sent) / (1024 * 1024)
            net_recv_mb = (current_net_io.bytes_recv - initial_net_io.bytes_recv) / (1024 * 1024)
            initial_net_io = current_net_io

            # Log metrics
            logging.info(f"CPU usage: {cpu_usage_percentage:.2f}%")
            logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB")
            logging.info(f"Disk I/O: Read={disk_read_mb:.2f} MB, Write={disk_write_mb:.2f} MB")
            logging.info(f"Network I/O: Sent={net_sent_mb:.2f} MB, Received={net_recv_mb:.2f} MB")
            
            
            await asyncio.sleep(20)  # Log every 60 seconds
    except asyncio.CancelledError:
        logging.info("Metrics logging task cancelled. Performing cleanup...")
## END BENCHMARKING ##

async def main():
    global running  # Declare that we are using the global variable
    connection_start_time = time.time()
    connection = await connect_robust("amqp://noms:tutorial@192.168.1.9:5672/integration_environment\r\n")
    connection_end_time = time.time()
    logging.info(f"Time taken to connect to RabbitMQ: {connection_end_time - connection_start_time:.6f} seconds")

    try:
        async with connection.channel() as channel:
            # Start logging system metrics
            metrics_task = asyncio.create_task(log_script_metrics())

            # Tail the log file continuously
            async with aiofiles.open('/var/log/asterisk/security.log', mode='r') as log_file:
                await log_file.seek(0, 2)  # Move the pointer to the end of the file
                while running:
                    line = await log_file.readline()
                    if line:
                        await process_log_line(line, channel)
                    else:
                        await asyncio.sleep(0.1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    finally:
        # Ensure the metrics logging task is properly cancelled
        metrics_task.cancel()
        try:
            await metrics_task
        except asyncio.CancelledError:
            logging.info("Metrics logging task successfully cancelled.")
        await connection.close()
        logging.info("Shutting down...")

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logging.info("Shutdown complete")
        logging.shutdown()
