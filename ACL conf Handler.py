import signal
import time
import subprocess
import psutil
import asyncio
import aiomysql
import logging

# Setup logging
logging.basicConfig(filename='/var/log/asterisk/acl.log', level=logging.DEBUG, format='%(asctime)s - %(message)s')

# MySQL (Docker) Database connection parameters
DB_HOST = '192.168.1.9'
DB_PORT = 3307
DB_USER = 'root'
DB_PASSWORD = 'tutorialRoot'
DB_NAME = 'dbAsterisk'

# Path to the ACL configuration file
ACL_CONFIG_PATH = "/etc/asterisk/acl.conf"

# Interval in seconds between checks
CHECK_INTERVAL = 25  # Check every 25 seconds for near-real-time updates

# Global flag for running state
running = True

def signal_handler(signum, frame):
    global running
    running = False
    logging.info("Shutdown signal received. Exiting...\r\n")

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def reload_acl_module():
    """Reloads the ACL module in Asterisk."""
    try:
        command = 'asterisk -rx "module reload acl"'
        subprocess.run(command, shell=True, check=True)
        logging.info("Asterisk ACL module reloaded successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to reload ACL module: {e}")

def update_acl_config(bad_ips):
    """Updates the ACL configuration file with the list of bad IPs"""
#    acl_start_time = time.time()
    try:
        with open(ACL_CONFIG_PATH, 'r+') as file:
            lines = file.readlines()
            file.seek(0)
            in_acl_block = False
            current_ips = []

            for line in lines:
                if '[aclblock]' in line:
                    in_acl_block = True
                if 'deny=' in line and in_acl_block:
                    current_ips = line.strip().split('=')[1].split(',')
                    line = f"deny={','.join(bad_ips)}\n"
                    in_acl_block = False  # End of ACL block
                file.write(line)
            file.truncate()

            added_ips = set(bad_ips) - set(current_ips)
            removed_ips = set(current_ips) - set(bad_ips)

            logging.info(f"Added IPs: {added_ips}")
            logging.info(f"Removed IPs: {removed_ips}")

        logging.info("ACL configuration updated successfully.")
    except Exception as e:
        logging.error(f"Failed to update ACL configuration: {e}")
    acl_end_time = time.time()
## BENCHMARKING ##
    logging.info(f"ACL configuration update time: {acl_end_time - acl_start_time:.6f} seconds")
    return acl_end_time - acl_start_time

async def fetch_all_ips():
    """Fetches all IPs and their scores from the database"""
    db_query_start_time = time.time()
    async with aiomysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME
    ) as conn:
        async with conn.cursor() as cursor:
            try:
                await cursor.execute("SELECT IP_address, score FROM ip_score")
                all_ips = await cursor.fetchall()
                for ip, score in all_ips:
                    logging.info(f"Fetched IP: {ip}, Score: {score}")
            except aiomysql.Error as e:
                logging.error(f"Database error: {e}")
                return []
    db_query_end_time = time.time()
    logging.info(f"Database IP fetch time: {db_query_end_time - db_query_start_time:.6f} seconds")
    return all_ips

## BENCHMARKING ##
 async def log_script_metrics(process, initial_disk_io, initial_net_io):
    """Log the script's CPU, memory, disk, and network usage metrics"""
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

    # Network usage (not exact, just approximation)
    current_net_io = psutil.net_io_counters()
    net_sent_mb = (current_net_io.bytes_sent - initial_net_io.bytes_sent) / (1024 * 1024)
    net_recv_mb = (current_net_io.bytes_recv - initial_net_io.bytes_recv) / (1024 * 1024)

    # Log metrics
    logging.info(f"CPU usage: {cpu_usage_percentage:.2f}%")
    logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB")
    logging.info(f"Disk I/O: Read={disk_read_mb:.2f} MB, Write={disk_write_mb:.2f} MB")
    logging.info(f"Network I/O: Sent={net_sent_mb:.2f} MB, Received={net_recv_mb:.2f} MB")

async def main():
    global running  # Declare that we are using the global variable
    process = psutil.Process()
    global_start_time = time.time()
    cpu_times_start = process.cpu_times()

    while running:
        cycle_start_time = time.time()
        cpu_times_cycle_start = process.cpu_times()

        all_ips = await fetch_all_ips()
        bad_ips = [ip for ip, score in all_ips if float(score) < 5.0]

        logging.info(f"IPs with score < 5: {bad_ips}")

        if bad_ips:
            acl_config_time = update_acl_config(bad_ips)
            reload_start_time = time.time()
            reload_acl_module()
            reload_end_time = time.time()
            logging.info(f"ACL reload time: {reload_end_time - reload_start_time:.6f} seconds")
            logging.info(f"Total ACL configuration and reload time: {acl_config_time + (reload_end_time - reload_start_time):.6f} seconds")
        else:
            logging.info("No IPs with score < 5. Skipping ACL update.")

        ## BENCHMARKING ##
        cycle_end_time = time.time()
        cpu_times_cycle_end = process.cpu_times()
        total_cycle_elapsed_time = cycle_end_time - cycle_start_time
        total_cycle_cpu_time = (cpu_times_cycle_end.user - cpu_times_cycle_start.user) + (cpu_times_cycle_end.system - cpu_times_cycle_start.system)

        if total_cycle_elapsed_time == 0:
            total_cycle_elapsed_time = 1e-6  # Set to a small non-zero value

        cpu_count = psutil.cpu_count()
        cpu_cycle_usage_percentage = (total_cycle_cpu_time / (total_cycle_elapsed_time * cpu_count)) * 100

        memory_info = process.memory_info()
        memory_rss_mb = memory_info.rss / (1024 * 1024)
        memory_vms_mb = memory_info.vms / (1024 * 1024)

        logging.info(f"Cycle processing time: {total_cycle_elapsed_time:.6f} seconds")
        logging.info(f"Total CPU time for cycle: {total_cycle_cpu_time:.6f} seconds")
        logging.info(f"CPU usage for cycle: {cpu_cycle_usage_percentage:.2f}% over {total_cycle_elapsed_time:.6f} seconds with {cpu_count} CPUs")
        logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB\r\n")

        initial_disk_io = psutil.disk_io_counters()
        initial_net_io = psutil.net_io_counters()
        await log_script_metrics(process, initial_disk_io, initial_net_io)
        ## END BENCHMARKING ##
        
        logging.info("Sleeping before next check...\r\n")
        await asyncio.sleep(CHECK_INTERVAL)

    ## BENCHMARKING ##
    global_end_time = time.time()
    total_runtime = global_end_time - global_start_time
    cpu_times_end = process.cpu_times()
    total_cpu_time = (cpu_times_end.user - cpu_times_start.user) + (cpu_times_end.system - cpu_times_start.system)
    total_elapsed_time = global_end_time - global_start_time
    cpu_usage_percentage = (total_cpu_time / (total_elapsed_time * cpu_count)) * 100

    logging.info(f"Script terminated. Total runtime: {total_runtime:.6f} seconds")
    logging.info(f"Total CPU time: {total_cpu_time:.6f} seconds")
    logging.info(f"Overall CPU usage: {cpu_usage_percentage:.2f}% over {total_elapsed_time:.6f} seconds with {cpu_count} CPUs")
    logging.info(f"Memory usage: RSS={memory_rss_mb:.2f} MB, VMS={memory_vms_mb:.2f} MB\r\n\r\n")
    ## END BENCHMARKING ##

if __name__ == '__main__':
    logging.debug('ACL configuration starting...\r\n')
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logging.info("Shutdown complete\r\n")
