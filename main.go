package main

import (
	"log"
	"net/http"
	"os"

	"example.com/09181/q009/dataroom"
)

func main() {
	// DATAROOM_DB 指向 JSON 快照文件；置空则为纯内存模式（重启不保留）。
	persistPath := os.Getenv("DATAROOM_DB")
	if persistPath == "" {
		persistPath = "data/dataroom.json"
	}
	svc := dataroom.New(persistPath)
	handler := dataroom.NewServer(svc)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	log.Printf("跨境并购资料室已启动，监听 :%s，持久化=%q", port, persistPath)
	if err := http.ListenAndServe(":"+port, handler); err != nil {
		log.Fatal(err)
	}
}
