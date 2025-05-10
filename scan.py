import argparse, time, urllib.parse, re, os, datetime
from typing import Dict, Tuple, List
import boto3
from botocore import UNSIGNED
from botocore.client import Config
import openai
import pandas as pd
"""

WHY I CHOSE THIS MODEL

Three models were tested:
1. GPT-4o 
2. GPT-4omini
3. GPT-4-turbo-2024-04-09

The worst performing model was GPT-4o, it had misclassified some of the images for random reasons (it depends on the venue, the date is passed, not an accurate depiction, etc...)

Between the other two, they both performed similarly with classifying all the files correctly as Y/N, both had shorter run times and similar accuracy. 
Ultimately, 4oturbo was chosen because it had a slightly higher classification accuracy on the categories when done through a macro and micro averaging 
which avoided the bad images and it was more consistent in responses. 
Howeveer, it's slightly more expensive to run and the slowest to run by a small margin (6.11mini, 7.2 4o base, 7.5 4o turbo). 

turbo was used for the final run.
"""

# ------------------------------- ENVIRONMENT VARIABLES -------------------------------
assistant_id  = os.getenv("ASSISTANT_ID")
openai.api_key = os.getenv("OPENAI_API_KEY")
OUTPUT_PREFIX = os.getenv("OUTPUT_PREFIX", "output/")
# -------------------------------- AWS S3 functions --------------------------------
def list_keys(bucket: str, prefix: str = ""):
    """
    Takes in a bucket name and prefix, and returns all keys in the bucket with prefix.

    """
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            yield obj["Key"]

def public_url(bucket: str, key: str) -> str:
    """
    Takes in a bucket name and name of a file, then returns the URL

    """

    return f"https://{bucket}.s3.amazonaws.com/{urllib.parse.quote(key)}"

def upload_public(bucket: str, local_path: str, s3_key: str):
    """
    Takes in a bucket name, file path, and S3 key, then uploads the file to S3.

    """
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED)) #creates a client 

    with open(local_path, "rb") as f: 
        s3.put_object( # writes the file to S3
            Bucket=bucket,
            Key=s3_key,
            Body=f,
            ACL="bucket-owner-full-control",
            ContentType="text/csv" if local_path.endswith(".csv") else "application/json"
        )
    print("Successfully uploaded to ", public_url(bucket, s3_key))


# -------------------------------- GPT Functions --------------------------------
def call_assistant(img_url: str) -> Tuple[str, float]:
    """
    Takes in an image URL and returns the assistant's reply and the time taken to get the reply.

    """
    start = time.time() # just for timing purposes to test viability of the assistant

    thread = openai.beta.threads.create()# creates a thread so that the assistant can remember the context of the conversation

    openai.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=[{"type": "image_url", "image_url": {"url": img_url}}],
    )
    run = openai.beta.threads.runs.create(thread_id=thread.id, assistant_id=assistant_id)

    while run.status not in {"completed", "failed", "cancelled", "expired"}:
        time.sleep(0.5)
        run = openai.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    if run.status != "completed":
        raise RuntimeError(f"Run aborted with status {run.status!r}")

    msgs = openai.beta.threads.messages.list(thread_id=thread.id, order="desc") # gets the messages in the thread
    assistant_msg = next(m for m in msgs.data if m.role == "assistant") # gets the assistant's message and not the user message
    return assistant_msg.content[0].text.value.strip(), time.time() - start

def parse_assistant_reply(msg_text: str) -> Dict[str, str]:
    """ Takes in the assistant's reply and returns a dictionary with the parsed values. 
    
    How it works: 
    
    1. Finds three set of numbers in the reply using regex (Decimals break the system and sends a valuerrorr, which writes a row with just the name of the file and null values and require manual checking)
    2. Checks if the first letter is a Y/N flag (If not, sends valueerror, writes a row with just the name of the file and null values and requires manual checking)
    3. The reasons are between the first and second set of numbers, and between the second and third set of numbers.
    4. Returns a dictionary with the parsed values. 
    Dictionary keys - all strings except is_safe, which is a boolean:
    - is_safe: True/False
    - safety_conf: Safety confidence score
    - safety_reason: Reason for safety confidence score
    - audience_cat: Audience category
    - aud_conf: Audience confidence score
    - aud_reason: Reason for audience confidence score

    No vision parameters were used 

    """

    num_pat  = r"[-+]?\d*\.?\d+" # regex to find numbers
    msg_text = re.sub(r'[\n\r]', ' ', msg_text) # replace newlines with spaces
    nums     = list(re.finditer(num_pat, msg_text)) # find all numbers in the text
    if len(nums) < 3 or len(nums) > 4:
        raise ValueError("Could not find three number sets in reply.")
    flag = re.search(r'^\W*([NY])', msg_text, re.IGNORECASE)
    if not flag:
        raise ValueError("No Y/N flag found at start of reply")
    try:
        int(nums[0].group()) # check if the first number is a float
        int(nums[1].group()) # check if the second number is a float
        int(nums[2].group()) # check if the third number is a float
        #check if positive
        if float(nums[0].group()) < 0 or float(nums[1].group()) < 0 or float(nums[2].group()) < 0:
            raise ValueError("Negative number found in reply")
    except ValueError:
        raise ValueError("Could not convert number to float")

    return {
        "is_safe":       flag.group(1).upper() == "Y",
        "safety_conf":   nums[0].group().strip(),
        "safety_reason": re.sub(r'^[^a-zA-Z]+|[^a-zA-Z]+$', '', # replace any non-alphabetic characters at the start and end of the string 
                                msg_text[nums[0].end():nums[1].start()].strip()),
        "audience_cat":  nums[1].group().strip(),
        "aud_conf":      nums[2].group().strip(),
        "aud_reason":    re.sub(r'^[^a-zA-Z]+|[^a-zA-Z]+$', '',
                                msg_text[nums[2].end():].strip()),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", help="Public bucket name (e.g. my-bkt-optisign)")
    ap.add_argument("--prefix", default="", help="Input key prefix, e.g. images/")
    ap.add_argument("--debug",metavar="RAW_REPLIES",help='sentence1,sentence2,...')
    args = ap.parse_args()
    bucket = args.bucket or os.getenv("INPUT_BUCKET")
    prefix = args.prefix or os.getenv("INPUT_PREFIX")

    # Debug parser
    if args.debug:
        tests = [t.strip() for t in args.debug.split(",")]
        df = pd.DataFrame(columns=[
            "raw_input", "is_safe", "safety_conf", "safety_reason",
            "audience_cat", "aud_conf", "aud_reason"
        ])
        for t in tests:
            try:
                parsed = parse_assistant_reply(t)
            except Exception as e:
                parsed = {
                    "is_safe": False, "safety_conf": 0, "safety_reason": e.__class__.__name__,
                    "audience_cat": 0, "aud_conf": 0, "aud_reason": e.__class__.__name__,
                }
            parsed["raw_input"] = t
            df = pd.concat([df, pd.DataFrame([parsed])], ignore_index=True)

        return

    # ------------------------ Main program ------------------------
    if not bucket:
        print("Either --bucket or --debug must be supplied.")
        return

    df = pd.DataFrame(columns=[
        "file_name", "is_safe", "safety_conf", "safety_reason",
        "audience_cat", "aud_conf", "aud_reason", "run_time"
    ])
    skipped = 0
    skipped_files = []

    for key in list_keys(bucket, prefix): #for all files in the bucket
        url = public_url(bucket, key) # gets the public URL of the file
        try:
            raw, dt = call_assistant(url) #calls api 
            parsed  = parse_assistant_reply(raw) # parses the reply

            if not parsed["is_safe"] or float(parsed["safety_conf"]) < 80:
                print(url.split("/")[-1], "ALERT_CS")
            
            if parsed["audience_cat"] in {"3", "4"} and parsed["is_safe"]:
                print(url.split("/")[-1], "NOTIFY_ACCOUNT_OWNER")
                print("One line summary: ", parsed["aud_reason"].replace("\n", " "))

            parsed.update({ # adds the file name and run time to the parsed dictionary
                "file_name": key[len(prefix):] if key.startswith(prefix) else key,
                "run_time":  dt,
            })
            
            df = pd.concat([df, pd.DataFrame([parsed])], ignore_index=True)

        except Exception as e:
            df = pd.concat([df, pd.DataFrame([{ # adds a row with the error message to the dataframe
                "file_name": key, "is_safe": False, "safety_conf": 0,
                "safety_reason": e.__class__.__name__, "audience_cat": 0,
                "aud_conf": 0, "aud_reason": e.__class__.__name__, "run_time": 0,
            }])], ignore_index=True)
            
            skipped += 1
            skipped_files.append(key)
            print(f"Skipping {key}: {e}")
    print("Number of skipped files: ", skipped)
    print("Skipped files: ", skipped_files)
    #Save locally then upload
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_name  = f"results_{ts}.csv"
    json_name = f"results_{ts}.json"

    df.to_csv(csv_name,  index=False)
    df.to_json(json_name, orient="records", lines=True)
    print()
    print("Local results saved ", csv_name, "and", json_name)

    #Upload to S3 under output/ 
    s3_key = os.path.join(OUTPUT_PREFIX, json_name)

    try:
        upload_public(bucket, json_name, s3_key)
    except Exception as e:
        print(f"Failed to upload {json_name}: {e}")


if __name__ == "__main__":
    main()
