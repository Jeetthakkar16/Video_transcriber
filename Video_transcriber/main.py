import whisper
import subprocess

#Converting seconds into miliseconds HH:MM:SS,mmm
def format_timestamp(seconds):
    milliseconds = int(seconds * 1000)

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    seconds = milliseconds // 1_000
    milliseconds %= 1_000

    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

#importing base model
model = whisper.load_model("base")


#generating result for video
result = model.transcribe("video.mp4",word_timestamps=True) #It will transcribe for each word

#creating and writing file in which we are going to save subtitles with their start --> end information for each sentence
with open("subtitles.srt", "w", encoding="utf-8") as file:

    for index, segment in enumerate(result["segments"], start=1):

        start = format_timestamp(segment["start"])
        end = format_timestamp(segment["end"])
        text = segment["text"].strip()

        file.write(f"{index}\n")
        file.write(f"{start} --> {end}\n")
        file.write(f"{text}\n\n")

# -----------------------------
# 4. Burn subtitles into video
# -----------------------------

subprocess.run([
    "ffmpeg",
    "-i", "video.mp4", #for input
    "-vf", "subtitles=subtitles.srt",   #vf means video-filter and subtitles is to select subtitles file
    "-c:a", "copy", #-c means copy and a means audio 
    "subtitled_video.mp4"   #create new file with the given name
], check=True)


print("Done!")
print("Created: subtitled_video.mp4")
