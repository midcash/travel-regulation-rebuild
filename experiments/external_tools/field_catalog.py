"""官方响应字段清单（2026-09-09 核对）。只用于观测覆盖率，不限制解析字段。"""

SOURCES = {
    "flight": "https://open.tuniu.com/mcp/docs/apidoc/mcp/flightMCP.html",
    "train": "https://open.tuniu.com/mcp/docs/apidoc/mcp/trainMCP.html",
    "hotel": "https://open.tuniu.com/mcp/docs/apidoc/mcp/hotelMCP.html",
    "poi": "https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch",
    "geocode": "https://lbs.amap.com/api/webservice/guide/api/georegeo",
    "driving_v3": "https://lbs.amap.com/api/webservice/guide/api/direction",
    "driving_v5": "https://lbs.amap.com/api/webservice/guide/api/newroute",
}


def fields(groups):
    return [
        f"{prefix}.{name}" for prefix, names in groups.items() for name in names.split()
    ]


FLIGHT = "flightNumber airlineCompany departureTime arrivalTime departureAirport arrivalAirport departureTerminal arrivalTerminal totalDuration flyTime type craftType"
HOTEL = "hotelId hotelName starName address business brandName commentScore lowestPrice firstPic cityName cityCode commentDigest meal refund roomName roomArea roomWindow distance"
SEATS = "gjrw rw rz swz tdz wz yw yz edz ydz dw ydw edw"
CATALOG = {
    "flight.searchLowestPriceFlight": fields(
        {
            "$": "successCode queryId totalPageNum data",
            "$.data[]": FLIGHT
            + " basePrice totalTax cabinClass remainingSeats shareFlightNo",
        }
    ),
    "flight.multiCabinDetails": fields(
        {
            "$": "successCode flightInfo cabinInfo",
            "$.flightInfo[]": FLIGHT + " punctualityRate",
            "$.cabinInfo[]": "cabinPriceId basePrice totalTax cabinClass cabinClassCodes buyCondition refundChangeRule baggageInfo mealCode reimbursementProof remainingSeats",
        }
    ),
    "train.searchLowestPriceTrain": fields(
        {
            "$": "successCode queryId totalPageNum data",
            "$.data[]": "trainNum departStationName destStationName trainType departureTime arrivalTime duration price seatAvailable",
            "$.data[].price": " ".join(s + "Price" for s in SEATS.split()),
            "$.data[].seatAvailable": " ".join(s + "Num" for s in SEATS.split()),
        }
    ),
    "train.queryTrainDetail": fields(
        {
            "$": "successCode data",
            "$.data": "trainInfo seatInfo",
            "$.data.trainInfo": "trainId trainNum trainType trainTypeCode trainTypeName departCityId destCityId departStationName departStationType departStationTypeCode destStationName destStationType destStationTypeCode departTime arriveTime saleStatus saleStatusId duration departsDate arriveDate",
            "$.data.seatInfo[]": "leftNumber seatId seatName price resId seatStatus",
        }
    ),
    "hotel.tuniuHotelSearch": fields(
        {
            "$": "displayHint message success queryId totalPageNum currentPageNum cityInfo poiInfo hotels",
            "$.cityInfo": "cityCode cityName",
            "$.poiInfo": "cityCode cityName poiCode poiName poiType lat lot",
            "$.hotels[]": HOTEL,
        }
    ),
    "hotel.tuniuHotelDetail": fields(
        {
            "$": "displayHint hotelId hotelName hotelNameEn starName firstPic address cityName cityCode business brandName commentScore commentDigest policies reviews roomTypes",
            "$.policies": "checkInTime checkOutTime cancelPolicy",
            "$.reviews": "score count",
            "$.roomTypes[]": "roomTypeId roomTypeName bedType maxOccupancy roomSize floor images ratePlans",
            "$.roomTypes[].ratePlans[]": "ratePlanName vendorRatePlanId rmbPrices preBookParam mealText cancelDesc count",
        }
    ),
    "amap.geocode": fields(
        {
            "$": "status info infocode count geocodes",
            "$.geocodes[]": "formatted_address country province citycode city district street number adcode location level",
        }
    ),
    "amap.reverse_geocode": fields(
        {
            "$": "status info infocode regeocode",
            "$.regeocode": "formatted_address addressComponent roads roadinters pois aois",
            "$.regeocode.addressComponent": "country province city citycode district adcode township towncode streetNumber businessAreas",
            "$.regeocode.addressComponent.streetNumber": "street number location direction distance",
            "$.regeocode.addressComponent.businessAreas[]": "location name id",
            "$.regeocode.roads[]": "id name distance direction location",
            "$.regeocode.roadinters[]": "distance direction location first_id first_name second_id second_name",
            "$.regeocode.pois[]": "id name type tel direction distance location address poiweight businessarea",
            "$.regeocode.aois[]": "id name adcode location area",
        }
    ),
    "amap.poi_search": fields(
        {
            "$": "status info infocode count pois",
            "$.pois[]": "id name location type typecode pname pcode cityname citycode adname adcode address parent distance business indoor navi photos children",
            "$.pois[].business": "business_area opentime_today opentime_week tel tag rating cost parking_type alias keytag rectag",
            "$.pois[].indoor": "indoor_map cpid floor truefloor",
            "$.pois[].navi": "navi_poiid entr_location exit_location gridcode",
            "$.pois[].photos[]": "title url",
            "$.pois[].children[]": "id name location address subtype typecode sname",
        }
    ),
    "amap.driving_v5": fields(
        {
            "$": "status info infocode count route",
            "$.route": "origin destination taxi_cost paths",
            "$.route.paths[]": "distance restriction steps cost",
            "$.route.paths[].cost": "duration tolls toll_distance toll_road traffic_lights",
            "$.route.paths[].steps[]": "instruction orientation road_name step_distance cost tmcs navi cities polyline",
            "$.route.paths[].steps[].cost": "duration tolls toll_distance toll_road",
            "$.route.paths[].steps[].tmcs[]": "tmc_status tmc_distance tmc_polyline",
            "$.route.paths[].steps[].navi": "action assistant_action",
            "$.route.paths[].steps[].cities[]": "adcode citycode city district",
            "$.route.paths[].steps[].cities[].district[]": "name adcode",
        }
    ),
    "amap.driving_v3": fields(
        {
            "$": "status info infocode count route",
            "$.route": "origin destination taxi_cost paths",
            "$.route.paths[]": "distance duration strategy tolls toll_distance restriction traffic_lights steps",
            "$.route.paths[].steps[]": "instruction orientation road distance tolls toll_distance toll_road duration polyline action assistant_action cities tmcs",
            "$.route.paths[].steps[].cities[]": "name citycode adcode districts",
            "$.route.paths[].steps[].cities[].districts[]": "name adcode",
            "$.route.paths[].steps[].tmcs[]": "distance status polyline",
        }
    ),
}
CATALOG["amap.poi_around"] = CATALOG["amap.poi_search"]
# ID 搜索的公开字段与关键字搜索略有区别。
CATALOG["amap.poi_detail"] = [
    path
    for path in CATALOG["amap.poi_search"]
    if path
    not in {
        "$.count",
        "$.pois[].children[].sname",
        "$.pois[].business.opentime_today",
        "$.pois[].business.opentime_week",
        "$.pois[].business.keytag",
        "$.pois[].business.rectag",
    }
] + ["$.pois[].atag"]
