#!/var/lib/asterisk/agi-bin/myenv/bin/python3

import sys
import aiomysql
import logging
import asyncio
import ipaddress
import pika
import json
import psutil
import time
from contextlib import asynccontextmanager, contextmanager

# Database connection + Asterisk Manager Interface + RabbitMQ
CONFIG = {
    'asterisk_db': {
        'user': 'asterisk',
        'password': 'password',
        'host': 'localhost',
        'db': 'asterisk'
    },
    'ami': {
        'host': '127.0.0.1',
        'port': 5038,
        'username': 'admin',
        'secret': 'admin'
    },
    'rabbitmq': {
        'host': '192.168.1.9',
        'port': 5672,
        'vhost': 'integration_environment',
        'credentials': ('noms', 'tutorial')
    },
    'allowed_ip_network': '192.168.1.0/24'
}

# Debug logging
logging.basicConfig(filename='/var/log/asterisk/agi-ongoing_calls.log', level=logging.DEBUG, format='%(asctime)s - %(message)s')
logging.getLogger('pika').setLevel(logging.WARNING)

@asynccontextmanager
async def get_db_connection(config):
    pool = await aiomysql.create_pool(**config)
    try:
        async with pool.acquire() as conn:
            yield conn
    finally:
        pool.close()
        await pool.wait_closed()

@asynccontextmanager
async def get_ami_connection(config):
    reader, writer = await asyncio.open_connection(config['host'], config['port'])
    try:
        login_command = f"Action: Login\r\nUsername: {config['username']}\r\nSecret: {config['secret']}\r\nEvents: off\r\n\r\n"
        writer.write(login_command.encode('utf-8'))
        await writer.drain()
        response = await reader.readuntil(b'\r\n\r\n')
#        logging.debug(f'AMI Login response: {response.decode("utf-8")}')
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()

@contextmanager
def get_rabbitmq_connection(config):
    credentials = pika.PlainCredentials(*config['credentials'])
    parameters = pika.ConnectionParameters(host=config['host'], port=config['port'], virtual_host=config['vhost'], credentials=credentials)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue='rs_queue', auto_delete=False, durable=True)
    try:
        yield channel
    finally:
        connection.close()

def is_ip_allowed(ip, network):
    return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(network)

async def get_active_calls(cursor, from_extension):
    query = "SELECT src_ip, from_extension, to_extension, channel FROM active_calls WHERE from_extension = %s"
    await cursor.execute(query, (from_extension,))
    return await cursor.fetchall()

async def insert_call(cursor, src_ip, from_extension, to_extension, channel):
    query = "INSERT INTO active_calls (src_ip, from_extension, to_extension, channel) VALUES (%s, %s, %s, %s)"
    await cursor.execute(query, (src_ip, from_extension, to_extension, channel))

async def delete_call(cursor, channel):
    query = "DELETE FROM active_calls WHERE channel = %s"
    await cursor.execute(query, (channel,))

async def ami_hangup_call(reader, writer, channel):
    try:
        hangup_start_time = time.time()
        hangup_command = f"Action: Hangup\r\nChannel: {channel}\r\n\r\n"
        writer.write(hangup_command.encode('utf-8'))
        await writer.drain()
        response = await reader.readuntil(b'\r\n\r\n')
        hangup_end_time = time.time()
        logging.info(f"Time taken to hang up call on channel {channel}: {hangup_end_time - hangup_start_time:.6f} seconds")
#        logging.debug(f'AMI Hangup response: {response.decode("utf-8")}')
    except Exception as e:
        logging.error(f"AMI connection or command error: {e}", exc_info=True)

def send_to_rabbitmq(channel, entityID, result):
    message = {
        "IP": entityID,
        "result": result
    }
    channel.basic_publish(
        exchange='IPREPUTATION',
        routing_key='rs_queue',
        body=json.dumps(message),
        properties=pika.BasicProperties(delivery_mode=2)
    )
    logging.info(f"Sent {message} to RabbitMQ")

async def check_and_hangup_calls(src_ip, from_extension, to_extension, channel):
    try:
        overall_start_time = time.time()
        process = psutil.Process()
        cpu_times_start = process.cpu_times()
        initial_disk_io = psutil.disk_io_counters()
        net_io_start = psutil.net_io_counters()

        db_conn_start_time = time.time()
        async with get_db_connection(CONFIG['asterisk_db']) as conn:
            db_conn_end_time = time.time()
            logging.info(f"Database connection acquisition time: {db_conn_end_time - db_conn_start_time:.6f} seconds")

            async with conn.cursor() as cursor:
                db_query_start_time = time.time()
                active_calls = await get_active_calls(cursor, from_extension)
                db_query_end_time = time.time()
                logging.info(f"Database query execution time: {db_query_end_time - db_query_start_time:.6f} seconds")

                logging.debug(f'Active calls for {from_extension}: {active_calls}')

                if not is_ip_allowed(src_ip, CONFIG['allowed_ip_network']) and \
                    (200 <= int(to_extension) <= 299 or 500 <= int(to_extension) <= 599):
                    logging.info(f'Hanging up call on channel: {channel} due to disallowed IP: {src_ip} for extension: {to_extension}')
                    async with get_ami_connection(CONFIG['ami']) as (reader, writer):
                        await ami_hangup_call(reader, writer, channel)
                    with get_rabbitmq_connection(CONFIG['rabbitmq']) as rabbitmq_channel:
                        send_to_rabbitmq(rabbitmq_channel, src_ip, "negative")
                elif len(active_calls) > 0:
                    logging.info(f'Hanging up call on channel: {channel} because {from_extension} is already on a call')
                    async with get_ami_connection(CONFIG['ami']) as (reader, writer):
                        await ami_hangup_call(reader, writer, channel)
                else:
                    logging.debug(f'Inserting call into database: src_ip={src_ip}, from_extension={from_extension}, to_extension={to_extension}, channel={channel}')
                    await insert_call(cursor, src_ip, from_extension, to_extension, channel)
                    await conn.commit()
                    logging.debug(f'Call inserted successfully')

        overall_end_time = time.time()
        cpu_times_end = process.cpu_times()
        current_disk_io = psutil.disk_io_counters()
        net_io_end = psutil.net_io_counters()

        total_elapsed_time = overall_end_time - overall_start_time
        total_cpu_time = (cpu_times_end.user - cpu_times_start.user) + (cpu_times_end.system - cpu_times_start.system)

        if total_elapsed_time == 0:
            total_elapsed_time = 1e-6

        cpu_count = psutil.cpu_count()
        cpu_usage_percentage = (total_cpu_time / (total_elapsed_time * cpu_count)) * 100

        memory_info = process.memory_info()
        memory_rss_mb = memory_info.rss / (1024 * 1024)
        memory_vms_mb = memory_info.vms / (1024 * 1024)

        disk_read_mb = (current_disk_io.read_bytes - initial_disk_io.read_bytes) / (1024 * 1024)
        disk_write_mb = (current_disk_io.write_bytes - initial_disk_io.write_bytes) / (1024 * 1024)
        net_io_sent_mb = (net_io_end.bytes_sent - net_io_start.bytes_sent) / (1024 * 1024)
        net_io_recv_mb = (net_io_end.bytes_recv - net_io_start.bytes_recv) / (1024 * 1024)

        logging.info(f"Overall processing time: {total_elapsed_time:.6f} seconds")
        logging.info(f"Total CPU time: {total_cpu_time:.6f} seconds")
        logging.info(f"CPU usage: {cpu_usage_percentage:.2f}% over {total_elapsed_time:.6f} seconds with {cpu_count} CPUs")
        logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB")
        logging.info(f"Disk usage: Read={disk_read_mb:.2f} MB, Write={disk_write_mb:.2f} MB")
        logging.info(f"Network I/O: Sent={net_io_sent_mb:.2f} MB, Received={net_io_recv_mb:.2f} MB")
        logging.info(f"AGI Script terminated\r\n\r\n")

    except aiomysql.MySQLError as err:
        logging.error(f"Database error: {err}", file=sys.stderr)
    except Exception as e:
        logging.error(f"Unexpected error: {e}", file=sys.stderr)

if __name__ == "__main__":
    logging.debug('AGI Script starting...')

    if len(sys.argv) != 5:
        logging.error('Invalid number of arguments. Expected 4 arguments but got {}'.format(len(sys.argv) - 1))
        sys.exit(1)

    src_ip, from_extension, to_extension, channel = sys.argv[1:5]

    if src_ip and from_extension and to_extension and channel:
        asyncio.run(check_and_hangup_calls(src_ip, from_extension, to_extension, channel))
    else:
        logging.error('Missing required arguments')
