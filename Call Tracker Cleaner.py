#!/var/lib/asterisk/agi-bin/myenv/bin/python3

import sys
import aiomysql
import logging
import asyncio
import psutil  # Metrics libary
import time

# Local database connection
asterisk_db_config = {
    'user': 'asterisk',
    'password': 'password',
    'host': 'localhost',
    'db': 'asterisk'
}

# Debug Logging
logging.basicConfig(filename='/var/log/asterisk/hangup_calls.log', level=logging.DEBUG, format='%(asctime)s - %(message)s')

async def delete_call(cursor, channel):
    query = "DELETE FROM active_calls WHERE channel = %s"
    await cursor.execute(query, (channel,))

async def main(channel):
    overall_start_time = time.time()
    process = psutil.Process()
    cpu_times_start = process.cpu_times()
    initial_disk_io = psutil.disk_io_counters()
    net_io_start = psutil.net_io_counters()

    pool = None
    try:
        logging.debug('Connecting to database')
        db_conn_start_time = time.time()
        pool = await aiomysql.create_pool(**asterisk_db_config)
        db_conn_end_time = time.time()
        logging.info(f"Database connection acquisition time: {db_conn_end_time - db_conn_start_time:.6f} seconds")

        async with pool.acquire() as conn:
            conn_acquire_time = time.time()
            logging.info(f"Database query execution time: {conn_acquire_time - db_conn_end_time:.6f} seconds")
            
            async with conn.cursor() as cursor:
                logging.debug(f'Deleting call from database: channel={channel}')
                await delete_call(cursor, channel)
                await conn.commit()
                logging.debug('Call deleted successfully')

    except aiomysql.MySQLError as err:
        logging.error(f"Database error: {err}", file=sys.stderr)
    except Exception as e:
        logging.error(f"Unexpected error: {e}", file=sys.stderr)
    finally:
        if pool:
            pool.close()
            await pool.wait_closed()

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
        memory_rss_mb = memory_info.rss / (1024 * 1024)
        memory_vms_mb = memory_info.vms / (1024 * 1024)

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
        logging.info(f"AGI Hangup Script terminated\r\n\r\n")

if __name__ == "__main__":
    logging.debug('Hangup Cleanup starting...')
    logging.debug(f'Script called with arguments: {sys.argv}')

    if len(sys.argv) != 2:
        logging.error('Invalid number of arguments. Expected 1 argument but got {}'.format(len(sys.argv) - 1))
        sys.exit(1)

    channel = sys.argv[1]
    logging.debug(f'Script called with channel={channel}')

    asyncio.run(main(channel))
