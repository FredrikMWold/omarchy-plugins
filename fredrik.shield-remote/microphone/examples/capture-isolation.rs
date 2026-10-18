#![allow(dead_code)]

#[path = "../src/adpcm.rs"]
mod adpcm;
#[path = "../src/consumer.rs"]
mod consumer;
#[path = "../src/pw.rs"]
mod pw;

use std::io::{self, BufRead, Write};
use std::sync::mpsc;
use std::time::Instant;

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::DEBUG)
        .with_writer(std::io::stderr)
        .init();
    let name = std::env::args().nth(1).expect("unique fixture source name");
    let (tx, rx) = mpsc::channel();
    let (shutdown, shutdown_rx) = pipewire::channel::channel();
    let worker = std::thread::spawn(move || {
        pw::run_pw_source(
            rx,
            0.0,
            8000,
            &name,
            "Audio isolation fixture",
            shutdown_rx,
            None,
        )
    });
    let mut held = None;
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line == "quit" {
            break;
        }
        if line == "release" {
            tx.send(held.take().expect("previously held audio chunk"))?;
        } else {
            let mut parts = line.split_whitespace();
            let command = parts.next().expect("fixture command");
            let value = parts.next().expect("sample value").parse::<i16>()?;
            let count = parts.next().expect("sample count").parse::<usize>()?;
            anyhow::ensure!(
                (1..=8000).contains(&count) && parts.next().is_none(),
                "invalid fixture frame"
            );
            let chunk = pw::AudioChunk {
                captured_at: Instant::now(),
                samples: vec![value; count],
            };
            match command {
                "frame" => tx.send(chunk)?,
                "hold" => {
                    anyhow::ensure!(held.is_none(), "audio chunk already held");
                    held = Some(chunk);
                }
                _ => anyhow::bail!("unknown fixture command: {command}"),
            }
        }
        println!("ok");
        io::stdout().flush()?;
    }
    shutdown.send(pw::Shutdown).expect("fixture shutdown");
    worker.join().expect("fixture source thread")?;
    Ok(())
}
