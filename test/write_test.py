import asyncio
import aiohttp
import time

URL = "http://localhost:8090/write-test"
URL = "http://localhost:8090/login"
CONCURRENT_REQUESTS = 10
TOTAL_REQUESTS = 100

async def fetch(session, url):
    async with session.get(url) as response:
        return await response.text()

async def main():
    responses = []
    async with aiohttp.ClientSession() as session:
        for i in range(0, TOTAL_REQUESTS, CONCURRENT_REQUESTS):
            tasks = [fetch(session, URL) for _ in range(CONCURRENT_REQUESTS)]
            responses.extend(await asyncio.gather(*tasks))
    return responses

if __name__ == "__main__":
    start_time = time.time()
    responses = asyncio.run(main())
    
    print (responses[5:10])
    
    end_time = time.time()

    elapsed_time = end_time - start_time
  
    rate = TOTAL_REQUESTS / elapsed_time

    
    print(f"Collected {len(responses)} responses")
    
    print(f"Rate: {rate:.2f} requests/sec")