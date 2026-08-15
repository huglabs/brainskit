package fixtures;

import java.util.ArrayList;
import java.util.List;

public class Scheduler {
    private final List<String> queue = new ArrayList<>();

    public void submit(String job) {
        queue.add(job);
    }

    public int pending() {
        return queue.size();
    }

    public String drain() {
        StringBuilder builder = new StringBuilder();
        for (String job : queue) {
            builder.append(job);
        }
        queue.clear();
        return builder.toString();
    }
}
