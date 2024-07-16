import pika
import logging
import signal
import json
import time
import asyncio
import aiomysql
import psutil
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(filename='/var/log/asterisk/agi-cdr2.log', level=logging.DEBUG, format='%(asctime)s - %(message)s')
logging.getLogger('pika').setLevel(logging.WARNING)

running = True

def signal_handler(signum, frame):
    global running
    running = False
    logging.info("Shutdown signal received. Exiting...")

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# RabbitMQ environment parameters, queue and exchange
credentials = pika.PlainCredentials('noms', 'tutorial')
parameters = pika.ConnectionParameters(
    host='192.168.1.9',
    port=5672,
    virtual_host='integration_environment',
    credentials=credentials,
    heartbeat=60,
    blocked_connection_timeout=300
)

# Local MariaDB database holding Call Detail Records (CDR)
asterisk_db_config = {
    'user': 'asterisk',
    'password': 'password',
    'host': 'localhost',
    'db': 'asterisk'
}
cdr_table = 'cdr'

async def create_connection():
    try:
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue='rs_queue', auto_delete=False, durable=True)
        return connection, channel
    except Exception as e:
        logging.error(f"Error creating RabbitMQ connection: {e}")
        raise

def close_connection(connection):
    if connection and connection.is_open:
        try:
            connection.close()
        except pika.exceptions.AMQPError as e:
            logging.error(f"Error closing connection: {e}")

def send_to_rabbitmq(channel, entityID, result):
    try:
        message = {
            "SRC": entityID,
            "result": result
        }
        channel.basic_publish(exchange='USERREPUTATION',
                              routing_key='rs_queue',
                              body=json.dumps(message),
                              properties=pika.BasicProperties(
                                  delivery_mode=2,
                              ))
        logging.info(f"Sent {message} to RabbitMQ")
    except Exception as e:
        logging.error(f"Error sending message to RabbitMQ: {e}")

async def evaluate_and_send(channel, last_uniqueid, pool):
    process = psutil.Process()
    overall_start_time = time.time()
    cpu_times_start = process.cpu_times()
    net_io_start = psutil.net_io_counters()

    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                query = f"""
                SELECT src, dst, dcontext, billsec, calldate, uniqueid
                FROM {cdr_table}
                WHERE uniqueid > %s
                ORDER BY uniqueid ASC
                """
                await cursor.execute("START TRANSACTION")
                await cursor.execute(query, (last_uniqueid,))
                rows = await cursor.fetchall()
                await cursor.execute("COMMIT")

                if rows:
                    new_uniqueid = max(row['uniqueid'] for row in rows)
                    logging.info(f"Fetched {len(rows)} rows. Processing from uniqueid {last_uniqueid} to {new_uniqueid}")
                    
                    for row in rows:
                        src = row['src']
                        dst = row['dst']
                        dcontext = row['dcontext']
                        billsec = row['billsec']
                        calldate = row['calldate']
                        uniqueid = row['uniqueid']
                        
                        logging.info(f"Processing call: src={src}, dst={dst}, dcontext={dcontext}, billsec={billsec}, calldate={calldate}, uniqueid={uniqueid}")
                        
                        if dcontext == 'from-external' and billsec < 30:
                            result = 'negative'
                        elif dcontext == 'from-internal' and dst.startswith('5'):
                            result = 'negative'
                        else:
                            result = 'positive'
                        
                        send_to_rabbitmq(channel, src, result)
                    
                    last_uniqueid = new_uniqueid
                    logging.info(f"Updated last_uniqueid to: {last_uniqueid}")
                else:
                    logging.info("No new rows fetched.")

    except aiomysql.MySQLError as err:
        logging.error(f"Database error: {err}")
    
    except pika.exceptions.AMQPError as err:
        logging.error(f"RabbitMQ error: {err}")
    
    except Exception as err:
        logging.error(f"Unexpected error: {err}")

    # Benchmarking metrics
    overall_end_time = time.time()
    cpu_times_end = process.cpu_times()
    total_elapsed_time = overall_end_time - overall_start_time
    total_cpu_time = (cpu_times_end.user - cpu_times_start.user) + (cpu_times_end.system - cpu_times_start.system)
    cpu_usage_percentage = (total_cpu_time / (total_elapsed_time * psutil.cpu_count())) * 100
    
    memory_info = process.memory_info()
    memory_rss_mb = memory_info.rss / (1024 * 1024)
    memory_vms_mb = memory_info.vms / (1024 * 1024)

    io_counters = process.io_counters()
    read_bytes_mb = io_counters.read_bytes / (1024 * 1024)
    write_bytes_mb = io_counters.write_bytes / (1024 * 1024)

    net_io_end = psutil.net_io_counters()
    sent_bytes_mb = (net_io_end.bytes_sent - net_io_start.bytes_sent) / (1024 * 1024)
    recv_bytes_mb = (net_io_end.bytes_recv - net_io_start.bytes_recv) / (1024 * 1024)

    logging.info(f"Overall processing time: {total_elapsed_time:.2f} seconds")
    logging.info(f"CPU usage: {cpu_usage_percentage:.2f}% over {total_elapsed_time:.2f} seconds")
    logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB")
    logging.info(f"Disk I/O: Read={read_bytes_mb:.2f} MB, Write={write_bytes_mb:.2f} MB")
    logging.info(f"Network I/O: Sent={sent_bytes_mb:.2f} MB, Received={recv_bytes_mb:.2f} MB")
    ## END BENCHMARKING ##
    
    return last_uniqueid


async def main():
    connection, channel = await create_connection()
    last_uniqueid = "0"
    
    pool = await aiomysql.create_pool(**asterisk_db_config)
    
    while running:
        try:
            last_uniqueid = await evaluate_and_send(channel, last_uniqueid, pool)
            logging.info(f"last_uniqueid updated to: {last_uniqueid}")
            await asyncio.sleep(5)  # Slight delay to ensure new rows are committed
        except pika.exceptions.AMQPConnectionError as e:
            logging.error(f"RabbitMQ connection error: {e}")
            await asyncio.sleep(5)
            close_connection(connection)
            connection, channel = await create_connection()
        except aiomysql.MySQLError as e:
            logging.error(f"Database connection error: {e}")
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            break
    
    close_connection(connection)
    pool.close()
    await pool.wait_closed()

if __name__ == '__main__':
    logging.info('CDR >> RabbitMQ starting...')
    try:
        asyncio.run(main())
    finally:
        logging.info("Shutting down")
