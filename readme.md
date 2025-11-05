Documentation

- I currently have an api service, for IRAS. Check GST Register.
- I want to build a streamlit app, whereby I will upload an excel and it will retrieve all the values in the excel file and make api call.
- The excel will contain a list of UEN in column A, and other data in column B onwards.
- After making the call, it will create 3 columns, `response-status` , `response-registrationId` and `json-reponse` and fill up the excel accordingly.
- I also want to have 1 more textbox whereby I can make individual uen call to the endpoint. This can help me make sure the api is working as well.
- Consider using async approach to allow scaling, however, take note of streamlit's limitation as well.
- Create a different event.loop from streamlit's to run async.
- Do not use `asyncio.run` `nest_asyncio`
- Current API Limit is 100 calls per hour. 
- I have clientID and clientSecret which I will set as an environment variable.
- Give me the python code as one `main.py` file.

Tech Stack
- Streamlit 1.40
- python 3.10
- aiohttp


Resources:
https://file.go.gov.sg/iras-checkgstregister-specs.pdf

sample python code:

```python

import requests

url = "https://apiservices.iras.gov.sg/iras/prod/GSTListing/SearchGSTRegistered"

payload = "REPLACE_BODY"
headers = {
    "X-IBM-Client-Id": "clientId",
    "X-IBM-Client-Secret": "clientSecret",
    "content-type": "application/json",
    "accept": "application/json"
}

response = requests.post(url, data=payload, headers=headers)

print(response.text)

```
Response:
```json
{
  "returnCode": 10,
  "registrationId": "",
  "gstRegistrationNumber": "199202892R",
  "name": "",
  "registeredFrom": "01/10/1994",
  "registeredTo": "",
  "status": "",
  "Remarks": ""
}
```