#!/var/lib/asterisk/agi-bin/myenv/bin/python3

import sys
import aiomysql
import logging
import asyncio
import time
import psutil  # Metrics

# MySQL (docker) connection
docker_db_config = {
    'user': 'root',
    'password': 'tutorialRoot',
    'host': '192.168.1.9',
    'port': 3307,
    'db': 'dbAsterisk'
}

# AMI
ami_host = '127.0.0.1'
ami_port = 5038
ami_username = 'admin'
ami_secret = 'admin'

# Debug logging
logging.basicConfig(filename='/var/log/asterisk/call_handler.log', level=logging.DEBUG, format='%(asctime)s - %(message)s')

# Global AMI connection
ami_reader, ami_writer = None, None

async def get_ami_connection():
    global ami_reader, ami_writer
    if ami_reader is None or ami_writer is None:
        ami_reader, ami_writer = await asyncio.open_connection(ami_host, ami_port)
        login_command = f"Action: Login\r\nUsername: {ami_username}\r\nSecret: {ami_secret}\r\nEvents: off\r\n\r\n"
        ami_writer.write(login_command.encode('utf-8'))
        await ami_writer.drain()
        response = await ami_reader.readuntil(b'\r\n\r\n')
#        logging.debug(f'AMI Login response: {response.decode("utf-8")}')
    return ami_reader, ami_writer

async def ami_hangup_call(reader, writer, channel):
    try:
        start_time = time.time()
 #       logging.debug(f'Attempting to hang up channel: {channel}')
        
        hangup_command = f"Action: Hangup\r\nChannel: {channel}\r\n\r\n"
        writer.write(hangup_command.encode('utf-8'))
        await writer.drain()

        response = await reader.readuntil(b'\r\n\r\n')
#        logging.debug(f'AMI Hangup response: {response.decode("utf-8")}')

        end_time = time.time()
        logging.info(f"AMI hangup time: {end_time - start_time:.6f} seconds")

    except Exception as e:
        logging.error(f"AMI connection or command error: {e}\r\n", exc_info=True)

async def get_user_score(cursor, user):
    query = "SELECT score FROM user_score WHERE user = %s"
    await cursor.execute(query, (user,))
    result = await cursor.fetchone()
    if result:
        return result[0]
    return None

async def check_and_hangup_calls(pool, from_extension, to_extension, channel):
    try:
        overall_start_time = time.time()
        process = psutil.Process()
        cpu_times_start = process.cpu_times()
        initial_disk_io = psutil.disk_io_counters()
        net_io_start = psutil.net_io_counters()

        start_time = time.time()
        logging.debug('Getting connection from pool')
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                connection_time = time.time()
                logging.info(f"Connection pool acquisition time: {connection_time - start_time:.6f} seconds")

                # Check from_extension score
                start_time = time.time()
                logging.debug(f'Checking score for from_extension: {from_extension}')
                from_score = await get_user_score(cursor, from_extension)
                query_time_from = time.time()
                logging.info(f"Query time for from_extension: {query_time_from - start_time:.6f} seconds")
                logging.debug(f'from_extension: {from_extension}, from_score: {from_score}')

                # Check to_extension score
                start_time = time.time()
                logging.debug(f'Checking score for to_extension: {to_extension}')
                to_score = await get_user_score(cursor, to_extension)
                query_time_to = time.time()
                logging.info(f"Query time for to_extension: {query_time_to - start_time:.6f} seconds")
                logging.debug(f'to_extension: {to_extension}, to_score: {to_score}')

                if (from_score is not None and from_score < 5) or (to_score is not None and to_score < 5):
                    logging.info(f'Hanging up call on channel: {channel}')
                    reader, writer = await get_ami_connection()
                    await ami_hangup_call(reader, writer, channel)
                    writer.close()
                    await writer.wait_closed()
                else:
                    logging.debug('Call does not need to be hung up')

        ## BENCHMARKING ##
        overall_end_time = time.time()
        cpu_times_end = process.cpu_times()
        current_disk_io = psutil.disk_io_counters()
        net_io_end = psutil.net_io_counters()

        total_elapsed_time = overall_end_time - overall_start_time
        total_cpu_time = (cpu_times_end.user - cpu_times_start.user) + (cpu_times_end.system - cpu_times_start.system)

        logging.debug(f"CPU times start: user={cpu_times_start.user:.4f}, system={cpu_times_start.system:.4f}")
        logging.debug(f"CPU times end: user={cpu_times_end.user:.4f}, system={cpu_times_end.system:.4f}")

        # Ensure total_elapsed_time is not zero to avoid division by zero
        if total_elapsed_time == 0:
            total_elapsed_time = 1e-6  # Set to a small non-zero value

        cpu_count = psutil.cpu_count()
        logging.info(f"Number of CPUs detected: {cpu_count}")

        cpu_usage_percentage = (total_cpu_time / (total_elapsed_time * cpu_count)) * 100

        memory_info = process.memory_info()
        memory_rss_mb = memory_info.rss / (1024 * 1024)  # Convertendo de bytes para MB
        memory_vms_mb = memory_info.vms / (1024 * 1024)  # Convertendo de bytes para MB

        disk_read_mb = (current_disk_io.read_bytes - initial_disk_io.read_bytes) / (1024 * 1024)
        disk_write_mb = (current_disk_io.write_bytes - initial_disk_io.write_bytes) / (1024 * 1024)
        net_io_sent_mb = (net_io_end.bytes_sent - net_io_start.bytes_sent) / (1024 * 1024)
        net_io_recv_mb = (net_io_end.bytes_recv - net_io_start.bytes_recv) / (1024 * 1024)

        logging.info(f"Overall processing time: {total_elapsed_time:.6f} seconds")
        logging.info(f"Total CPU time: {total_cpu_time:.6f} seconds")
        logging.info(f"CPU usage: {cpu_usage_percentage:.2f}% over {total_elapsed_time:.6f} seconds with {cpu_count} CPUs")
        if cpu_usage_percentage > 100:
            logging.info("High CPU usage percentage is due to the very short elapsed time.")
        logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB")
        logging.info(f"Disk usage: Read={disk_read_mb:.2f} MB, Write={disk_write_mb:.2f} MB")
        logging.info(f"Network I/O: Sent={net_io_sent_mb:.2f} MB, Received={net_io_recv_mb:.2f} MB")
        ## END BENCHMARKING ##
        logging.info(f"AGI Script terminated\r\n\r\n")
        

    
    except aiomysql.MySQLError as err:
        logging.error(f"Database error: {err}")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
    finally:
        await pool.wait_closed()

if __name__ == "__main__":
    logging.debug('AGI Script starting...')

    # Argumentos passados pelo plano de discagem
    logging.debug(f'Script called with arguments: {sys.argv}')

    if len(sys.argv) != 4:
        logging.error('Invalid number of arguments. Expected 3 arguments but got {}'.format(len(sys.argv) - 1))
        sys.exit(1)

    from_extension = sys.argv[1]
    to_extension = sys.argv[2]
    channel = sys.argv[3]

    logging.debug(f'Script called with from_extension={from_extension}, to_extension={to_extension}, channel={channel}')

    if from_extension and to_extension and channel:
        loop = asyncio.get_event_loop()
        pool = loop.run_until_complete(aiomysql.create_pool(**docker_db_config))
        try:
            loop.run_until_complete(check_and_hangup_calls(pool, from_extension, to_extension, channel))
        finally:
            pool.close()
            loop.run_until_complete(pool.wait_closed())
            loop.close()
    else:
        logging.error('Missing required arguments')
