"""Synthetic wire responses, not historical supplier recordings."""
import json
import httpx

REQUEST = {"origin": "杭州", "destination": "三亚", "meeting_location": "三亚湾红树林国际会议中心",
           "meeting_at": "2099-09-19T09:00+08:00", "transport_mode": "flight"}
FLIGHT = {"mode": "flight", "origin": "杭州", "destination": "三亚", "departure_date": "2099-09-18"}
STAY = {"destination": "三亚", "check_in": "2099-09-18", "check_out": "2099-09-21"}


def rpc(request, payload):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"],
                                    "result": {"structuredContent": payload}})


def supplier(request):
    if request.method == "POST":
        name = json.loads(request.content)["params"]["name"]
        payloads = {
            "searchLowestPriceFlight": {"data": [{"flightNumber": "HU1", "arrivalAirport": "凤凰",
                "departureTime": "2099-09-18 16:05:00", "arrivalTime": "2099-09-18 18:55:00"}]},
            "searchLowestPriceTrain": {"data": [{"trainNum": "G123", "departStationName": "杭州东",
                "destStationName": "三亚", "departureTime": "2099-09-18 06:00:00",
                "arrivalTime": "2099-09-18 22:00:00", "price": {"二等座": "888.50"}}]},
            "multiCabinDetails": {"flightInfo": {"flightNumber": "HU1", "arrivalAirport": "凤凰",
                "arrivalTerminal": "T1"}, "cabinInfo": [{"basePrice": "123.45010001", "totalTax": None}]},
            "queryTrainDetail": {"data": {"trainInfo": {"trainNum": "G123"},
                "seatInfo": [{"seatName": "二等座", "price": "123.45010001", "leftNumber": None}]}},
            "tuniuHotelSearch": {"hotels": [{"hotelId": "34548", "hotelName": "海韵酒店",
                "address": "三亚湾路168号", "lowestPrice": "321.45010001"}]},
            "tuniuHotelDetail": {"hotelId": 34548, "roomTypes": [{"ratePlans": [
                {"rmbPrices": "321.45010001", "cancelDesc": "不可退"}]}]},
        }
        if name not in payloads:
            raise AssertionError("Unexpected supplier operation: " + name)
        return rpc(request, payloads[name])
    path, params = request.url.path, request.url.params
    if path.endswith("/text"):
        places = {
            "凤凰机场T1": ("airport", "机场路", "109.1000,18.3000"),
            "海韵酒店": ("hotel", "三亚湾路168号", "109.1100,18.3100"),
            REQUEST["meeting_location"]: ("meeting", "凤凰路155号", "109.1200,18.3200"),
        }
        keyword = params["keywords"]
        identifier, address, location = places[keyword]
        return httpx.Response(200, json={"status": "1", "pois": [{"id": identifier, "name": keyword,
            "address": address, "cityname": "三亚市", "location": location}]})
    if path.endswith("/detail"):
        return httpx.Response(200, json={"status": "1", "pois": [{"id": params["id"], "address": "三亚湾路168号"}]})
    if path.endswith("/geo"):
        return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "109.1100,18.3100"}]})
    if path.endswith("/driving"):
        first = params["origin"] == "109.1000,18.3000"
        return httpx.Response(200, json={"status": "1", "route": {"origin": params["origin"],
            "destination": params["destination"], "taxi_cost": "88", "paths": [{"id": "R1",
            "distance": "21000" if first else "5000", "duration": "1260" if first else "942",
            "cost": {"tolls": "12"} if first else {}, "steps": []}]}})
    raise AssertionError("Unexpected supplier path: " + path)
